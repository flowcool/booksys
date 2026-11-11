"""Entity-oriented catalogue. Files and external effects stay outside transactions."""
from __future__ import annotations

from contextlib import nullcontext
import copy
from dataclasses import dataclass, field
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import psycopg
from psycopg import sql
import tempfile
from typing import Any, TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    from library.stage_tasks import Obligation
    from library.operational_state import OperationalCompletion

from library.primitives import LibraryError, TABLES, empty, sync_dir, validate
from library.codec import decode, encode
from library.postgres import Database, DeadlineConnection, guards, CommitUnknown


from contextvars import ContextVar

# Only Catalog.project_pending scopes this exact immutable record and change.
_projection: ContextVar[tuple[str,str] | None] = ContextVar('decision_projection',default=None)


from library.stage_model import Change as Change, PreparedResult as PreparedResult, SourceProgress as SourceProgress


RELATIONS = {
    'edition_work': ('editions', 'works'), 'variant_edition': ('variants', 'editions'),
    'variant_asset': ('variants', 'assets'), 'asset_origin': ('assets', 'origins'),
    'origin_parent': ('origins', 'origins'), 'selected_variant': ('editions', 'variants'),
    'artifact_asset': ('artifacts', 'assets'), 'artifact_edition': ('artifacts', 'editions'),
    'operation_edition': ('operations', 'editions'),
    'edition_publication_asset': ('editions', 'assets'),
    'identity_work': ('assets', 'works'), 'identity_edition': ('assets', 'editions'),
    'identity_variant': ('assets', 'variants'),
}

QUERY_COLUMNS = ('source_ref', 'pid', 'fid', 'work_id', 'edition_id', 'asset_id', 'state', 'next_attempt', 'identity_status', 'technical_status', 'resolution_status', 'disposition', 'admission_status')
ACTIONS = ('identify', 'identify_work', 'override', 'exclude', 'defer', 'select', 'merge_work',
           'separate_edition', 'resolve', 'resolution_result', 'admit', 'publish', 'retire')


def receipt_schema(db: DeadlineConnection) -> None:
    """Keep catalogue revision reads independent of growing control history."""
    db.execute("CREATE INDEX commit_receipts_catalogue_committed ON commit_receipts(key) "
               "WHERE catalogue AND state='committed'")


def query_values(kind: str, record: dict[str, Any]) -> tuple[Any, ...]:
    identity = record.get('identity') or {}
    return (record.get('source'), record.get('pid'), record.get('fid'),
            record.get('work') if kind == 'editions' else identity.get('work'),
            record.get('edition') or identity.get('edition'),
            record.get('asset') or (record.get('sha256') if kind == 'origins' else None),
            record.get('state') or record.get('availability'), record.get('automation', {}).get('next_attempt'),
            'contested' if record.get('contested') else 'identified' if identity or
            record.get('work_result', {}).get('status') == 'identified' else 'unknown',
            record.get('status'), record.get('resolution', {}).get('status'), record.get('disposition'),
            record.get('admission', {}).get('status'))


def destination_schema(db: DeadlineConnection) -> None:
    """Install once, atomically, before selecting the indexed-query candidate.

    Expression indexes cover existing rows, COPY imports, updates and deletes.
    The function is versioned: never replace its body under existing indexes.
    """
    db.execute("""CREATE FUNCTION destination_memberships_v1(payload text) RETURNS text[]
        LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
        SECURITY INVOKER SET search_path=pg_catalog AS $body$
        DECLARE record jsonb := payload::jsonb->'record'; nested jsonb;
                field text; targets text[];
        BEGIN
            IF record ? '$booksys' THEN
                SELECT jsonb_object_agg(pair->>0,pair->1) INTO record
                FROM jsonb_array_elements(record->'$booksys'->1) pair;
            END IF;
            targets := ARRAY[record->>'target',record->>'destination'];
            FOREACH field IN ARRAY ARRAY['publication','admission','provisional'] LOOP
                nested := record->field;
                IF nested->'$booksys'->>0 = 'map' THEN
                    SELECT jsonb_object_agg(pair->>0,pair->1) INTO nested
                    FROM jsonb_array_elements(nested->'$booksys'->1) pair;
                END IF;
                targets := array_append(targets,nested->>'target');
            END LOOP;
            RETURN array_remove(targets,NULL);
        END;
        $body$""")
    for kind in TABLES:
        db.execute(f'CREATE INDEX {kind}_destination_memberships_v1 ON {kind} '
                   'USING gin (destination_memberships_v1(payload))')


def schema(db: DeadlineConnection) -> None:
    db.execute('CREATE TABLE meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
               'authority_id TEXT COLLATE \"C\" NOT NULL, schema_version INTEGER NOT NULL, global_revision BIGINT NOT NULL, '
               'imported_source_digest TEXT COLLATE \"C\" NOT NULL, ledger TEXT COLLATE \"C\" NOT NULL, case_scope_revision INTEGER NOT NULL DEFAULT 0, '
               'ready INTEGER NOT NULL DEFAULT 0, source_semantic TEXT COLLATE \"C\" NOT NULL, '
               'required_runtime TEXT COLLATE \"C\" NOT NULL, created_at TEXT COLLATE \"C\" NOT NULL)')
    for table in TABLES:
        db.execute(f'CREATE TABLE {table} (id TEXT COLLATE \"C\" PRIMARY KEY, revision INTEGER NOT NULL, '
                   'payload TEXT COLLATE \"C\" NOT NULL, source_ref TEXT COLLATE \"C\", pid BIGINT, fid BIGINT, work_id TEXT COLLATE \"C\", '
                   'edition_id TEXT COLLATE \"C\", asset_id TEXT COLLATE \"C\", state TEXT COLLATE \"C\", next_attempt DOUBLE PRECISION, identity_status TEXT COLLATE \"C\", technical_status TEXT COLLATE \"C\", '
                   'resolution_status TEXT COLLATE \"C\", disposition TEXT COLLATE \"C\", admission_status TEXT COLLATE \"C\")')
        for column in QUERY_COLUMNS:
            db.execute(f'CREATE INDEX {table}_{column} ON {table}({column},id)')
    destination_schema(db)
    db.execute('CREATE TABLE asset_flags(asset TEXT COLLATE \"C\" PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE, '
               'provisional_published INTEGER NOT NULL CHECK(provisional_published IN (0,1)))')
    for name, (source, target) in RELATIONS.items():
        db.execute(f'CREATE TABLE {name} (source TEXT COLLATE \"C\" NOT NULL REFERENCES {source}(id) '
                   'DEFERRABLE INITIALLY DEFERRED, target TEXT COLLATE \"C\" NOT NULL '
                   f'REFERENCES {target}(id) DEFERRABLE INITIALLY DEFERRED, PRIMARY KEY(source,target))')
        db.execute(f'CREATE INDEX {name}_target ON {name}(target,source)')
    db.execute('CREATE TABLE ledger_records(sequence INTEGER PRIMARY KEY,id TEXT COLLATE \"C\" UNIQUE NOT NULL,'
               'target TEXT COLLATE \"C\",action TEXT COLLATE \"C\" NOT NULL,request_id TEXT COLLATE \"C\",payload TEXT COLLATE \"C\" NOT NULL)')
    db.execute('CREATE TABLE decision_reservations(sequence BIGINT PRIMARY KEY,id TEXT COLLATE \"C\" UNIQUE NOT NULL, '
               "target TEXT COLLATE \"C\",payload TEXT COLLATE \"C\" NOT NULL,status TEXT COLLATE \"C\" NOT NULL CHECK(status IN ('reserved','indexed','projected')),scope TEXT NOT NULL)")
    db.execute('CREATE INDEX reservation_target ON decision_reservations(target,sequence)')
    for column in ('target', 'action', 'request_id'):
        db.execute(f'CREATE INDEX ledger_{column} ON ledger_records({column},sequence)')
    db.execute('CREATE TABLE decision_editions(decision TEXT COLLATE \"C\" NOT NULL REFERENCES ledger_records(id),'
               'edition TEXT COLLATE \"C\" NOT NULL,PRIMARY KEY(decision,edition))')
    db.execute('CREATE INDEX decision_edition ON decision_editions(edition,decision)')
    db.execute('CREATE TABLE decision_pieces(piece TEXT COLLATE \"C\" NOT NULL,decision TEXT COLLATE \"C\" NOT NULL '
               'REFERENCES ledger_records(id),PRIMARY KEY(piece,decision))')
    db.execute('CREATE TABLE preview_bindings(id TEXT COLLATE \"C\" PRIMARY KEY,asset TEXT COLLATE \"C\" NOT NULL)')
    db.execute('CREATE TABLE edition_claims(field TEXT COLLATE \"C\" NOT NULL,value TEXT COLLATE \"C\" NOT NULL,edition TEXT COLLATE \"C\" NOT NULL '
               'REFERENCES editions(id) DEFERRABLE INITIALLY DEFERRED,PRIMARY KEY(field,value,edition))')
    db.execute('CREATE INDEX edition_claim_owner ON edition_claims(edition)')
    db.execute('CREATE TABLE decision_claims(field TEXT COLLATE \"C\" NOT NULL,value TEXT COLLATE \"C\" NOT NULL,decision TEXT COLLATE \"C\" NOT NULL '
               'REFERENCES ledger_records(id),PRIMARY KEY(field,value,decision))')
    db.execute('CREATE TABLE case_bindings(id TEXT COLLATE \"C\" PRIMARY KEY,kind TEXT COLLATE \"C\" NOT NULL,target TEXT COLLATE \"C\" NOT NULL)')
    db.execute('CREATE INDEX case_target ON case_bindings(kind,target)')
    db.execute('CREATE TABLE attachments (digest TEXT COLLATE \"C\" PRIMARY KEY, encoding TEXT COLLATE \"C\" NOT NULL, '
               'original_length INTEGER NOT NULL, relative_path TEXT COLLATE \"C\" NOT NULL)')

    db.execute('CREATE TABLE operator_rows(id TEXT COLLATE \"C\" PRIMARY KEY,source_ref TEXT COLLATE \"C\" NOT NULL,title TEXT COLLATE \"C\" NOT NULL,'
               'author TEXT COLLATE \"C\" NOT NULL,isbn TEXT COLLATE \"C\" NOT NULL,series TEXT COLLATE \"C\" NOT NULL,status TEXT COLLATE \"C\" NOT NULL,'
               'transfer TEXT COLLATE \"C\" NOT NULL,intake TEXT COLLATE \"C\" NOT NULL,identity TEXT COLLATE \"C\" NOT NULL,publication TEXT COLLATE \"C\" NOT NULL,'
               'reason TEXT COLLATE \"C\" NOT NULL,ocr TEXT COLLATE \"C\" NOT NULL,search_text TEXT COLLATE \"C\" NOT NULL,payload TEXT COLLATE \"C\" NOT NULL)')
    for column in ('source_ref', 'title', 'series', 'status', 'transfer', 'intake', 'identity', 'publication'):
        db.execute(f'CREATE INDEX operator_rows_{column} ON operator_rows({column},source_ref,id)')
    db.execute('CREATE TABLE operator_sort(id TEXT COLLATE \"C\" NOT NULL REFERENCES operator_rows(id) ON DELETE CASCADE, '
               'field TEXT COLLATE \"C\" NOT NULL,value BYTEA NOT NULL,PRIMARY KEY(id,field))')
    db.execute('CREATE INDEX operator_sort_order ON operator_sort(field,value,id)')


class Store:
    def __init__(self, directory: Path, *, authority: str, database: Database | None = None):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', authority):
            raise LibraryError('Invalid catalogue authority')
        if directory.is_symlink():
            raise LibraryError('Catalogue directory must not be a symlink')
        self.directory = directory.resolve()
        self._database = database or Database.configured(self.directory)
        from library.media_paths import media_root
        self.payloads = media_root('BOOKSYS_ATTACHMENT_ROOT', self.directory/'catalogue/payloads')
        self.authority = authority

    @classmethod
    def create(cls, directory: Path, *, database: Database, authority: str,
               state: dict[str, Any], source_digest: str = '', semantic: str = '',
               import_identity: dict[str,Any] | None = None) -> Store:
        """Explicit empty-target initialization for import and isolated fixtures."""
        validate(state)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        store = cls(directory, authority=authority, database=database)
        store.payloads.mkdir(mode=0o700,parents=True,exist_ok=True)
        prepared: dict[str,list[tuple[str,str,dict[str,int],tuple[Any,...]]]]={kind:[] for kind in TABLES}
        for kind in TABLES:
            for ident,record in state[kind].items():
                store._validate_record(kind,ident,record)
                payload,attachments=store.prepare(record)
                prepared[kind].append((ident,payload,attachments,query_values(kind,record)))
        with database.connection(write=True) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(database.schema)))
            schema(db)
            db.execute('INSERT INTO meta VALUES (1,%s,1,%s,%s,%s,%s,%s,%s,%s,%s)',
                       (authority,state['revision'],source_digest,encode(state['ledger']),
                        import_identity['source_meta']['case_scope_revision'] if import_identity else 0,
                        int(import_identity is None),semantic,'postgresql>=18.6',datetime.now().isoformat()))
            if import_identity is not None:
                db.execute('CREATE TABLE migration_import(singleton INTEGER PRIMARY KEY CHECK(singleton=1),identity TEXT NOT NULL,phase TEXT NOT NULL)')
                db.execute("INSERT INTO migration_import VALUES(1,%s,'initializing')",(encode(import_identity),))
                db.execute('CREATE TABLE migration_phases(name TEXT PRIMARY KEY,digest TEXT NOT NULL)')
                from library.activity_store import schema as activity_schema
                activity_schema(db)
            db.execute('CREATE TABLE conflict_guards(key TEXT COLLATE \"C\" PRIMARY KEY)')
            from library.postgres import guard_schema
            guard_schema(db,database.schema)
            db.execute("CREATE TABLE commit_receipts(key TEXT COLLATE \"C\" PRIMARY KEY,digest TEXT COLLATE \"C\" NOT NULL, "
                       "state TEXT COLLATE \"C\" NOT NULL CHECK(state IN ('registered','committed','aborted')), "
                       "catalogue BOOLEAN NOT NULL DEFAULT false,receipt TEXT COLLATE \"C\")")
            receipt_schema(db)
            db.execute('CREATE TABLE source_registry(id TEXT COLLATE \"C\" PRIMARY KEY,payload TEXT COLLATE \"C\" NOT NULL)')
            db.execute('CREATE TABLE source_inputs(source TEXT COLLATE \"C\" NOT NULL REFERENCES source_registry(id), '
                       'entity TEXT COLLATE \"C\" NOT NULL,input_revision TEXT COLLATE \"C\" NOT NULL,method TEXT COLLATE \"C\" NOT NULL,guard TEXT COLLATE \"C\" NOT NULL, '
                       'progress TEXT COLLATE \"C\",binding TEXT COLLATE \"C\",PRIMARY KEY(source,entity))')
            from library.operational_state import schema as operational_schema
            operational_schema(db)
            for kind in TABLES:
                for ident,payload,attachments,query in prepared[kind]:
                    for aid, length in attachments.items():
                        db.execute('INSERT INTO attachments VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                                   (aid, 'utf8-legacy-cover', length, 'payloads/'+aid))
                    db.execute(f'INSERT INTO {kind} VALUES (' + ','.join(['%s']*(3+len(QUERY_COLUMNS))) + ')',
                               (ident, state['revision'], payload, *query))
            for kind in TABLES:
                for ident, record in state[kind].items():
                    store._relations(db, kind, ident, record)
                    store._validate_links(db, kind, ident, record)
            store._refresh_rows(db, {kind: set(state[kind]) for kind in TABLES})
        from library.stage_tasks import StageTasks
        StageTasks.upgrade(store, importing=import_identity is not None)
        from library.launcher_access import install
        install(database)
        return store

    def check(self, db: DeadlineConnection, *, importing: bool = False) -> tuple[int, list[str]]:
        row = db.execute('SELECT authority_id,schema_version,global_revision,ledger,ready,required_runtime FROM meta WHERE singleton=1').fetchone()
        if (not row or row[0] != self.authority or row[1] != 1 or row[4] not in ((0,1) if importing else (1,))
                or row[5] != 'postgresql>=18.6'):
            raise LibraryError('Catalogue authority or schema mismatch')
        ledger = decode(row[3])
        if not isinstance(ledger, list) or len(set(ledger)) != len(ledger):
            raise LibraryError('Invalid catalogue ledger head')
        count = db.execute("SELECT count(*) FROM commit_receipts WHERE catalogue AND state='committed'").fetchall()[0][0]
        return int(row[2]+count), ledger

    def _freshness(self, db: DeadlineConnection) -> str:
        rows = db.execute("SELECT key,digest FROM commit_receipts WHERE catalogue AND state='committed' ORDER BY key").fetchall()
        return hashlib.sha256(encode([self.authority, [list(row) for row in rows]]).encode()).hexdigest()

    @property
    def revision(self) -> int:
        with self._database.connection() as db:
            return self.check(db)[0]

    def _attachment(self, value: str) -> str:
        data = value.encode('utf-8')
        ident = hashlib.sha256(data).hexdigest()
        self.payloads.mkdir(mode=0o700, exist_ok=True)
        if self.payloads.is_symlink() or self.payloads.stat().st_mode & 0o077:
            raise LibraryError('Invalid payload directory')
        target = self.payloads / ident
        if target.exists():
            if target.is_symlink() or target.read_bytes() != data:
                raise LibraryError('Attachment digest mismatch')
            return ident
        fd, name = tempfile.mkstemp(dir=self.payloads, prefix='.payload-')
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.link(temporary, target)
            sync_dir(self.payloads)
        finally:
            temporary.unlink()
        return ident

    def prepare(self, record: dict[str, Any]) -> tuple[str, dict[str, int]]:
        # Covers are entity fields, not user-defined tags. A separate envelope
        # prevents collisions with arbitrary legacy extension dictionaries.
        attachments: dict[str, int] = {}
        payload = dict(record)
        cover = payload.get('cover')
        if isinstance(cover, str) and cover:
            ident = self._attachment(cover)
            attachments[ident] = len(cover.encode('utf-8'))
            del payload['cover']
            return encode({'record': payload, 'cover': ident}), attachments
        return encode({'record': payload}), attachments

    def restore(self, payload: str, *, media: bool = True) -> dict[str, Any]:
        value = decode(payload)
        record: dict[str, Any] = value['record']
        if 'cover' in value and media:
            ident = value['cover']
            if not isinstance(ident, str) or len(ident) != 64 or any(c not in '0123456789abcdef' for c in ident):
                raise LibraryError('Invalid attachment reference')
            path = self.payloads / ident
            if path.is_symlink():
                raise LibraryError('Unsafe attachment')
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != ident:
                raise LibraryError('Attachment digest mismatch')
            record['cover'] = data.decode('utf-8')
        return record

    def get(self, kind: str, ident: str, *, media: bool = True) -> dict[str, Any] | None:
        self._kind(kind)
        with self._database.connection() as db:
            self.check(db)
            row = db.execute(f'SELECT payload FROM {kind} WHERE id=%s', (ident,)).fetchone()
            return self.restore(row[0], media=media) if row else None

    def ids(self, kind: str) -> list[str]:
        self._kind(kind)
        with self._database.connection() as db:
            self.check(db)
            return [row[0] for row in db.execute(f'SELECT id FROM {kind} ORDER BY id')]

    def page(self, kind: str, *, filters: dict[str, Any] | None = None,
             cursor: str | None = None, limit: int = 50, media: bool = False) -> dict[str, Any]:
        self._kind(kind)
        filters = filters or {}
        if type(limit) is not int or not 1 <= limit <= 100 or set(filters) - set(QUERY_COLUMNS):
            raise LibraryError('Invalid catalogue query')
        shape = hashlib.sha256(encode([kind, filters]).encode()).hexdigest()
        after = ''
        token = None
        if cursor is not None:
            try:
                if len(cursor) > 2048:
                    raise ValueError('cursor bound')
                token = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
                if set(token) != {'revision', 'shape', 'after'} or token['shape'] != shape or type(token['after']) is not str:
                    raise ValueError('cursor shape')
                after = token['after']
            except (ValueError, TypeError, KeyError) as exc:
                raise LibraryError('Invalid catalogue cursor') from exc
        with self._database.connection() as db:
            revision, _ = self.check(db)
            freshness = self._freshness(db)
            if token is not None and token['revision'] != freshness:
                raise LibraryError('Stale catalogue cursor')
            conditions = ['id>%s'] + [column + ' IS NOT DISTINCT FROM %s' for column in sorted(filters)]
            rows = db.execute(f'SELECT id,payload FROM {kind} WHERE ' + ' AND '.join(conditions)
                              + ' ORDER BY id LIMIT %s',
                              (after, *(filters[key] for key in sorted(filters)), limit + 1)).fetchall()
            items = [{'id': ident, 'record': self.restore(payload, media=media)} for ident, payload in rows[:limit]]
            next_cursor = base64.urlsafe_b64encode(json.dumps({'revision': freshness, 'shape': shape,
                'after': items[-1]['id']}).encode()).decode() if len(rows) > limit else None
            return {'revision': revision, 'items': items, 'cursor': next_cursor}

    def scan(self, kind: str, *, after: str = '', limit: int = 100,
             filters: dict[str, Any] | None = None) -> list[tuple[str, dict[str, Any]]]:
        """Bounded key scan for periodic discovery; a new pass covers insertions.

        Each page is current. Unrelated writes do not invalidate the cursor.
        Business work must still prepare and verify its own relevant closure.
        """
        self._kind(kind)
        filters = filters or {}
        if not 1 <= limit <= 256 or set(filters) - set(QUERY_COLUMNS):
            raise LibraryError('Invalid discovery page')
        conditions = ['id>%s'] + [column + ' IS NOT DISTINCT FROM %s' for column in sorted(filters)]
        with self._database.connection() as db:
            self.check(db)
            rows = db.execute(f'SELECT id,payload FROM {kind} WHERE ' + ' AND '.join(conditions)
                + ' ORDER BY id LIMIT %s', (after, *(filters[key] for key in sorted(filters)), limit)).fetchall()
            return [(ident, self.restore(payload, media=False)) for ident, payload in rows]

    def decision(self, ident: str) -> dict[str, Any] | None:
        with self._database.connection() as db:
            self.check(db)
            row = db.execute('SELECT payload FROM ledger_records WHERE id=%s', (ident,)).fetchone()
            return decode(row[0]) if row else None

    @staticmethod
    def _index_decision(db: DeadlineConnection, record: dict[str, Any]) -> None:
        from library.primitives import digest
        previous = db.execute('SELECT id FROM ledger_records ORDER BY sequence DESC LIMIT 1').fetchone()
        count = db.execute('SELECT count(*) FROM ledger_records').fetchall()[0][0]
        existing = db.execute('SELECT payload FROM ledger_records WHERE sequence=%s', (record.get('sequence'),)).fetchone()
        if existing:
            if existing[0] != encode(record):
                raise LibraryError('Indexed decision differs from immutable evidence')
            return
        if (record.get('sequence') != count + 1 or record.get('previous') != (previous[0] if previous else None)
                or record.get('id') != digest({key: value for key, value in record.items() if key != 'id'})):
            raise LibraryError('Invalid decision ledger suffix')
        db.execute('INSERT INTO ledger_records VALUES (%s,%s,%s,%s,%s,%s)',
                   (record['sequence'], record['id'], record.get('target'), record['action'],
                    record.get('request_id'), encode(record)))
        for field in ('isbn', 'reference'):
            value = record.get('values', {}).get('edition', {}).get(field)
            if value:
                db.execute('INSERT INTO decision_claims VALUES (%s,%s,%s)', (field, str(value), record['id']))
        db.cursor().executemany('INSERT INTO decision_editions VALUES (%s,%s)',
                       [(record['id'], eid) for eid in sorted(set(record.get('affected_editions', [])))])
        values = record.get('values', {})
        pieces = {proof['page'] for proof in values.get('proof', [])} if record['action'] == 'resolve' else set()
        if record['action'] == 'resolve':
            pieces.update(values.get('notices', []))
        elif record['action'] == 'identify_work':
            pieces.update(proof['page'] for proof in values.get('evidence', {}).get('pages', []))
        db.cursor().executemany('INSERT INTO decision_pieces VALUES (%s,%s)', [(piece, record['id']) for piece in sorted(pieces)])

    def retained_pieces(self) -> set[str]:
        with self._database.connection() as db:
            self.check(db)
            return {row[0] for row in db.execute('SELECT DISTINCT piece FROM decision_pieces')}

    def bind_preview(self, preview: str, asset: str) -> None:
        with self._database.connection(write=True,seconds=.5) as db:
            self.check(db)
            db.execute('INSERT INTO preview_bindings VALUES (%s,%s) ON CONFLICT(id) DO UPDATE SET asset=excluded.asset', (preview, asset))

    def preview_asset(self, preview: str) -> str | None:
        with self._database.connection() as db:
            self.check(db)
            row = db.execute('SELECT asset FROM preview_bindings WHERE id=%s', (preview,)).fetchone()
            return row[0] if row else None

    def reserve_decision(self, payload: dict[str, Any], *, conflict: dict[str, Any] | None = None) -> dict[str, Any]:
        """Reserve one chain position durably before serializing immutable evidence."""
        from library.primitives import digest
        from library.locking import acquisition_deadline, bounded_acquisition
        import time
        value = hashlib.sha256(encode({'payload':payload,'conflict':conflict}).encode()).hexdigest()
        key = 'decision-append:' + hashlib.sha256(encode(payload).encode()).hexdigest()
        keys = self._decision_keys(payload)
        if conflict:
            closure: dict[str,dict[str,Any]] = {kind:{ident:{} for ident in ids} for kind,ids in conflict['entities'].items()}
            for kind,ids in conflict['refs'].items():
                closure.setdefault(kind,{}).update({ident:{} for ident in ids})
            keys.update(self._guard_keys(closure,conflict['claims']))
        stop = min(acquisition_deadline.get() or float('inf'),time.monotonic()+10)
        with bounded_acquisition(stop):
            self._register(key,value)
            with self._database.connection(write=True) as db:
                receipt = db.execute('SELECT digest,state,receipt FROM commit_receipts WHERE key=%s FOR UPDATE',(key,)).fetchone()
                if not receipt or receipt[0] != value or receipt[1] == 'aborted':
                    raise LibraryError('Decision reservation was fenced')
                if receipt[1] == 'committed':
                    retained: dict[str, Any] = decode(receipt[2])
                    return retained
                if conflict is not None:
                    current = self.working_set(conflict['action'],conflict['refs'],media=False,_db=db)
                    keys.update(self._guard_keys(current,conflict['claims']))
                if payload.get('case_version'):
                    case_scope = self.working_set('identify_work',{'assets':[payload['target']]},media=False,_db=db)
                    keys.update(self._guard_keys(case_scope))
                guards(db,keys)
                if conflict is not None:
                    from library.conflicts import verify
                    verify(self,db,conflict)
                if payload.get('case_version') and self._case(db,payload['target'])['version'] != payload['case_version']:
                    raise LibraryError('Result conflict: case inputs changed')
                last = db.execute('SELECT sequence,id,status FROM decision_reservations ORDER BY sequence DESC LIMIT 1').fetchone()
                if last and last[2] == 'reserved':
                    raise LibraryError('Reconcile interrupted decision reservation before confirmation')
                indexed = db.execute('SELECT sequence,id FROM ledger_records ORDER BY sequence DESC LIMIT 1').fetchone()
                sequence,previous = (indexed[0]+1,indexed[1]) if indexed else (1,None)
                record = {**payload,'schema':2 if payload['action'] in ('resolve','resolution_result') else 1,
                          'sequence':sequence,'previous':previous}
                record['id'] = digest(record)
                db.execute("INSERT INTO decision_reservations VALUES (%s,%s,%s,%s,'reserved',%s)",
                           (sequence,record['id'],record.get('target'),encode(record),encode(sorted(keys-{'decision-chain'}))))
                db.execute("UPDATE commit_receipts SET state='committed',receipt=%s WHERE key=%s",(encode(record),key))
                return record

    @staticmethod
    def _decision_keys(record: dict[str, Any]) -> set[str]:
        keys = {'decision-chain'}
        if record.get('target'):
            keys.add('decisions:assets:' + record['target'])
        editions = set(record.get('affected_editions', []))
        if record.get('values',{}).get('edition_id'):
            editions.add(record['values']['edition_id'])
        keys.update('decisions:editions:' + eid for eid in editions)
        keys.update(Store._guard_keys({
            'assets':{record['target']:{}} if record.get('target') else {},
            'editions':{eid:{} for eid in editions}}))
        for field in ('isbn','reference'):
            claim = record.get('values',{}).get('edition',{}).get(field)
            if claim:
                keys.add('claim:'+field+':'+str(claim))
        return keys

    def pending_decisions(self) -> list[dict[str, Any]]:
        """Exact durable append payloads whose immutable evidence needs recovery."""
        with self._database.connection() as db:
            self.check(db)
            return [decode(row[0]) for row in db.execute(
                "SELECT payload FROM decision_reservations WHERE status='reserved' ORDER BY sequence")]

    def _check_decision_reservations(self, db: DeadlineConnection, keys: set[str], *, projection: str | None = None) -> None:
        active=_projection.get()
        sequence=None
        if projection is not None and active is not None:
            row=db.execute('SELECT sequence FROM decision_reservations WHERE id=%s',(projection,)).fetchone()
            if row is not None:sequence=row[0]
        for ident,scope,future in db.execute("SELECT id,scope,sequence FROM decision_reservations WHERE status IN ('reserved','indexed')"):
            if sequence is not None and future>sequence:continue
            if ident != projection and keys & set(decode(scope)):
                raise LibraryError('Result conflict: decision scope is reserved')

    def _completion_projection(self, db: DeadlineConnection, ident: str, attempt: tuple[str,int], token: dict[str,Any]) -> bool:
        row=db.execute('SELECT payload FROM ledger_records WHERE id=%s',(ident,)).fetchone()
        if row is None:return False
        record=decode(row[0])
        if (record.get('action')!='resolution_result' or record.get('completion_task')!={'key':attempt[0],'generation':attempt[1]}
            or record.get('completion_conflict')!=token):return False
        task_row=db.execute('SELECT payload FROM stage_tasks WHERE key=%s',(attempt[0],)).fetchone()
        task=decode(task_row[0]) if task_row else {}
        parent_row=db.execute('SELECT payload FROM ledger_records WHERE id=%s',(record['values']['decision'],)).fetchone()
        parent=decode(parent_row[0]) if parent_row else {}
        if (task.get('stage')!='copies' or task.get('generation')!=attempt[1]
            or task.get('entity')!='decision:'+record['values']['decision']
            or task.get('input_revision')!=record['values']['decision']
            or parent.get('action')!='resolve' or parent.get('target')!=record.get('target')
            or record.get('completion_parent')!={name:parent.get(name) for name in ('id','expected_after','affected_editions')}
            or record.get('affected_editions')!=parent.get('affected_editions',[])):
            return False
        reservation=db.execute('SELECT payload,status,scope FROM decision_reservations WHERE id=%s',(ident,)).fetchone()
        if reservation is None or reservation[0]!=row[0] or reservation[1]!='indexed':return False
        closure: dict[str,dict[str,Any]]={kind:{key:{} for key in ids} for kind,ids in token['entities'].items()}
        for kind,ids in token['refs'].items():closure.setdefault(kind,{}).update({key:{} for key in ids})
        expected=(self._guard_keys(closure,token['claims'])|self._decision_keys(record))-{'decision-chain'}
        return expected<=set(decode(reservation[2]))

    def _verify_completion_result(self,token: dict[str,Any],attempt: tuple[str,int]) -> None:
        from library.conflicts import verify
        with self._database.connection() as db:
            rows=db.execute("SELECT id FROM ledger_records WHERE action='resolution_result' ORDER BY sequence DESC").fetchall()
            projection=next((row[0] for row in rows if self._completion_projection(db,row[0],attempt,token)),None)
            verify(self,db,token,projection=projection,completion_attempt=attempt)

    def index_decision(self, record: dict[str, Any]) -> None:
        value = hashlib.sha256(encode(record).encode()).hexdigest()
        key = 'decision-index:' + record['id']
        keys = self._decision_keys(record)
        import time
        from library.locking import acquisition_deadline, bounded_acquisition
        stop = min(acquisition_deadline.get() or float('inf'),time.monotonic()+10)
        with bounded_acquisition(stop):
            try:
                self._register(key,value)
                with self._database.connection(write=True) as db:
                    receipt = db.execute('SELECT digest,state FROM commit_receipts WHERE key=%s FOR UPDATE',(key,)).fetchone()
                    if not receipt or receipt[0] != value:
                        raise LibraryError('Commit key input mismatch')
                    if receipt[1] == 'committed':
                        return
                    if receipt[1] == 'aborted':
                        raise LibraryError('Commit key was fenced aborted')
                    retained_scope = db.execute('SELECT scope FROM decision_reservations WHERE id=%s',(record['id'],)).fetchone()
                    if retained_scope:
                        keys.update(decode(retained_scope[0]))
                    guards(db, keys)
                    self.check(db)
                    reservation = db.execute('SELECT payload FROM decision_reservations WHERE id=%s',(record['id'],)).fetchone()
                    if not reservation or reservation[0] != encode(record):
                        raise LibraryError('Decision differs from its durable reservation')
                    self._index_decision(db, record)
                    db.execute("UPDATE decision_reservations SET status='indexed' WHERE id=%s",(record['id'],))
                    db.execute("UPDATE commit_receipts SET state='committed',catalogue=true,receipt=%s WHERE key=%s",
                               (encode({'decision':record['id']}),key))

            except CommitUnknown:
                if self.outcome(key,input_digest=value)['state'] != 'committed':
                    raise

    def decision_head(self) -> dict[str,Any] | None:
        """Read the exact indexed append predecessor without a ledger scan."""
        with self._database.connection() as db:
            self.check(db)
            row=db.execute('SELECT payload FROM ledger_records ORDER BY sequence DESC LIMIT 1').fetchone()
            return decode(row[0]) if row else None

    def decisions(self, *, target: str | None = None, edition: str | None = None,
                  request_id: str | None = None, action: str | None = None, after: int = 0) -> list[dict[str, Any]]:
        with self._database.connection() as db:
            self.check(db)
            conditions: list[str] = ['sequence>%s']
            values: list[Any] = [after]
            for column, value in (('target', target), ('request_id', request_id), ('action', action)):
                if value is not None:
                    conditions.append(column + '=%s')
                    values.append(value)
            if edition is not None:
                conditions.append('id IN (SELECT decision FROM decision_editions WHERE edition=%s)')
                values.append(edition)
            return [decode(row[0]) for row in db.execute('SELECT payload FROM ledger_records WHERE '
                    + ' AND '.join(conditions) + ' ORDER BY sequence', values)]

    def resolution_state(self, asset: str, *, values: dict[str, Any] | None = None,
                         options: bool = False) -> dict[str, Any]:
        with self._database.connection() as db:
            return self._resolution_state(db, asset, values=values, options=options)

    def _resolution_state(self, db: DeadlineConnection, asset: str, *,
                          values: dict[str, Any] | None, options: bool) -> dict[str, Any]:
        refs: dict[str, list[str]] = {'assets': [asset], 'editions': [], 'works': []}
        values = values or {}
        for kind, field in (('editions', 'edition_id'), ('works', 'work_id')):
            if values.get(field):
                refs[kind].append(values[field])
        self.check(db)
        for field in ('isbn', 'reference'):
            value = values.get('edition', {}).get(field)
            if value:
                refs['editions'].extend(row[0] for row in db.execute(
                    'SELECT edition FROM edition_claims WHERE field=%s AND value=%s', (field, str(value))))
                refs['assets'].extend(row[0] for row in db.execute('SELECT target FROM ledger_records WHERE id IN '
                    '(SELECT decision FROM decision_claims WHERE field=%s AND value=%s)', (field, str(value))))
        if options:
            for kind in ('works', 'editions'):
                refs[kind].extend(row[0] for row in db.execute(f'SELECT id FROM {kind} ORDER BY id LIMIT 100'))
            # Existing form exposes all active ownership. Bounds refuse it
            # explicitly; they never silently truncate that ownership view.
            variants = db.execute('SELECT id,payload FROM variants ORDER BY id LIMIT 10001').fetchall()
            if len(variants) > 10000:
                raise LibraryError('Ownership view incomplete: more than10000 variants')
            refs['variants'] = [row[0] for row in variants if self.restore(row[1], media=False).get('active', True)]
        from library.conflicts import snapshot
        from library.primitives import WorkingState
        claims = {field:[str(values['edition'][field])] for field in ('isbn','reference')
                  if values.get('edition',{}).get(field)}
        state, conflict = snapshot(self,db,'resolve',refs,claims,media=False)
        row = db.execute('SELECT payload FROM assets WHERE id=%s', (asset,)).fetchone()
        target = self.restore(row[0]) if row else None
        if target is not None:
            state['assets'][asset] = target
        if options and len(encode({kind: state[kind] for kind in ('variants', 'works', 'editions')}).encode()) > 2 * 1024**2:
            raise LibraryError('Ownership view incomplete: response exceeds2MiB')
        # Overlay unapplied decisions for this closure. No global replay or write.
        from library.effects import apply_effect
        records: dict[int, dict[str, Any]] = {}
        revision, applied = self.check(db)
        if state['revision'] != revision:
            raise LibraryError('Stale resolution snapshot')
        for key in state['assets']:
            for row in db.execute('SELECT payload FROM ledger_records WHERE target=%s AND sequence>%s ORDER BY sequence',
                                  (key, len(applied))):
                record = decode(row[0]); records[record['sequence']] = record
        from library.decision_model import verify_record_evidence
        for _, record in sorted(records.items()):
            if record.get('completion_task'):continue
            verify_record_evidence(self, record)
            apply_effect(state, record, replay=True)
        result = WorkingState(state)
        result.conflict = conflict
        return result

    def case(self, asset: str) -> dict[str, Any]:
        with self._database.connection() as db:
            return self._case(db,asset)

    def _case(self, db: DeadlineConnection, asset: str) -> dict[str, Any]:
        from library.primitives import digest
        from library.conflicts import snapshot
        state, token = snapshot(self, db, 'identify_work', {'assets': [asset]}, {})
        record = state['assets'][asset]
        identity = record.get('identity')
        inputs = {'asset': record, 'origins': {oid: {key: value for key, value in origin.items() if key != 'observed_at'}
                  for oid, origin in state['origins'].items() if oid in record['origins']},
                  'edition': state['editions'].get(identity['edition']) if identity else None,
                  'scope': token['fingerprint']}
        return {'id': digest({'asset': asset, 'kind': 'asset_review'}), 'asset': asset, 'version': digest(inputs),
                'reason': 'identity_contested' if record.get('contested') else 'identity_or_edition_unknown' if not identity
                else 'metadata_or_variant_review', 'evidence': inputs}

    def _refresh_rows(self, db: DeadlineConnection, changed: dict[str, set[str]]) -> None:
        from library.projection import operator_row
        affected = set(changed.get('origins', set()))
        assets = set(changed.get('assets', set()))
        editions = set(changed.get('editions', set()))
        for wid in changed.get('works', set()):
            editions.update(row[0] for row in db.execute('SELECT id FROM editions WHERE work_id=%s', (wid,)))
        for eid in editions:
            assets.update(row[0] for row in db.execute('SELECT id FROM assets WHERE edition_id=%s', (eid,)))
        for aid in assets:
            affected.update(row[0] for row in db.execute('SELECT id FROM origins WHERE asset_id=%s', (aid,)))
        def get(kind: str, ident: str | None) -> dict[str, Any]:
            row = db.execute(f'SELECT payload FROM {kind} WHERE id=%s', (ident,)).fetchone()
            return self.restore(row[0], media=False) if row else {}
        for oid in sorted(affected):
            origin = get('origins', oid)
            if not origin:
                db.execute('DELETE FROM operator_rows WHERE id=%s', (oid,))
                continue
            asset = get('assets', origin.get('sha256'))
            identity = asset.get('identity') or {}
            value = operator_row(oid, origin, asset, get('editions', identity.get('edition')),
                                 get('works', identity.get('work')), {})
            columns = ('source_id', 'title', 'author', 'isbn', 'series', 'status', 'transfer',
                       'intake', 'identity', 'publication', 'reason', 'ocr')
            db.execute('INSERT INTO operator_rows VALUES (' + ','.join(['%s'] * 15) + ') ON CONFLICT(id) DO UPDATE SET ' + ','.join(c+'=excluded.'+c for c in ('source_ref','title','author','isbn','series','status','transfer','intake','identity','publication','reason','ocr','search_text','payload')),
                       (oid, *(value[key] for key in columns),
                        ' '.join(str(value[key]) for key in ('title', 'author', 'isbn', 'series', 'reason')).casefold(),
                        json.dumps(value, ensure_ascii=False, allow_nan=False)))

            from library.sorting import storage_key
            for name in columns[1:]:
                db.execute('INSERT INTO operator_sort VALUES (%s,%s,%s) '
                           'ON CONFLICT(id,field) DO UPDATE SET value=excluded.value',
                           (oid,name,storage_key(str(value[name]))))

    def compatibility_counts(self, db: DeadlineConnection) -> dict[str, int]:
        """Aggregate fixed flags in SQLite; decode only escaped rare maps."""
        published = db.execute('SELECT count(DISTINCT target) FROM edition_publication_asset').fetchall()[0][0]
        classified, holding = (db.execute('SELECT count(*) FROM assets WHERE admission_status=%s', (status,)).fetchall()[0][0]
                               for status in ('classified', 'holding'))
        provisional = db.execute('SELECT coalesce(sum(provisional_published),0) FROM asset_flags').fetchall()[0][0]
        return {'confirmed_assets': published, 'classified_assets': classified,
                'holding_assets': holding, 'provisional_assets': provisional}

    def operator_page(self, *, filters: dict[str, str] | None = None, search: str = '',
                      sort: str = 'title', cursor: str | None = None, limit: int = 50, descending: bool = False, page: int = 1) -> dict[str, Any]:
        import re
        filters = filters or {}
        allowed = ('status', 'transfer', 'intake', 'identity', 'publication')
        if (type(page) is not int or not 1 <= page <= 200000 or set(filters) - set(allowed) or sort not in ('title', 'author', 'isbn', 'series', 'reason', 'ocr', *allowed)
                or type(descending) is not bool or type(search) is not str or len(search) > 240 or type(limit) is not int or not 1 <= limit <= 50
                or any(type(v) is not str or len(v) > 240 for v in filters.values())):
            raise LibraryError('Invalid operator query')
        shape = hashlib.sha256(encode([filters, search, sort, limit, descending]).encode()).hexdigest()
        offset, expected = (page - 1) * limit, None
        if cursor:
            try:
                token = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True)) if len(cursor) <= 2048 else {}
                if (set(token) != {'shape', 'revision', 'offset'} or token['shape'] != shape
                        or type(token['offset']) is not int or not 0 <= token['offset'] <= 10000000):
                    raise ValueError('cursor')
                offset, expected = token['offset'], token['revision']
            except (ValueError, TypeError, KeyError) as exc:
                raise LibraryError('Invalid operator cursor') from exc
        with self._database.connection() as db:
            revision, _ = self.check(db)
            freshness = self._freshness(db)
            if expected is not None and expected != freshness:
                raise LibraryError('Stale operator cursor')
            conditions, values = [], []
            for name in sorted(filters):
                conditions.append(name + '=%s')
                values.append(filters[name])
            if search:
                conditions.append('strpos(search_text,%s)>0')
                values.append(search.casefold())
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
            direction = ' DESC' if descending else ''
            groups = db.execute('WITH matches AS (SELECT source_ref,min(id) AS representative FROM operator_rows'
                + where + ' GROUP BY source_ref) SELECT m.source_ref,r.' + sort + ',r.payload FROM matches m '
                'JOIN operator_rows r ON r.id=m.representative JOIN operator_sort s ON s.id=r.id '
                'AND s.field=%s ORDER BY s.value' + direction + ',m.source_ref LIMIT %s OFFSET %s',
                (*values,sort,limit+1,offset)).fetchall()
            result = []
            for source, _, representative in groups[:limit]:
                rows = db.execute('SELECT payload FROM operator_rows WHERE source_ref=%s ORDER BY id LIMIT 51',
                                  (source,)).fetchall()
                result.append({'id': source, 'representative': json.loads(representative), 'rows': [json.loads(row[0]) for row in rows[:50]],
                               'children_after': json.loads(rows[49][0])['id'] if len(rows) > 50 else None})
            counts = db.execute('SELECT count(DISTINCT source_ref),count(*) FROM operator_rows' + where, values).fetchall()[0]
            facets = {name: [row[0] for row in db.execute(f'SELECT DISTINCT {name} FROM operator_rows ORDER BY {name}')]
                      for name in allowed}
            next_cursor = base64.urlsafe_b64encode(json.dumps({'shape': shape, 'revision': freshness,
                         'offset': offset + limit}).encode()).decode() if len(groups) > limit else None
            return {'schema': 1, 'revision': revision, 'groups': result, 'cursor': next_cursor,
                    'counts': {'sources': counts[0], 'origins': counts[1]}, 'facets': facets, 'page': page,
                    'pages': max(1, (counts[0] + limit - 1) // limit)}

    def operator_children(self, source: str, *, after: str = '', limit: int = 50) -> dict[str, Any]:
        import re
        if (not isinstance(source, str) or not re.fullmatch('[0-9a-f]{64}', source)
                or not isinstance(after, str) or len(after) > 128 or type(limit) is not int or not 1 <= limit <= 50):
            raise LibraryError('Invalid source child query')
        with self._database.connection() as db:
            revision, _ = self.check(db)
            rows = db.execute('SELECT id,payload FROM operator_rows WHERE source_ref=%s AND id>%s ORDER BY id LIMIT %s',
                              (source, after, limit + 1)).fetchall()
            return {'revision': revision, 'rows': [json.loads(row[1]) for row in rows[:limit]],
                    'children_after': rows[limit - 1][0] if len(rows) > limit else None}

    def working_set(self, action: str, refs: dict[str, list[str]], *, media: bool = True,
                    _db: DeadlineConnection | None = None) -> dict[str, Any]:
        """Resolve relation closure in a consistent snapshot, with exact legacy media.

        Global revision protects membership changes, including newly competing
        claims. Full ledger replay remains an explicit recovery operation.
        """
        if action not in ACTIONS:
            raise LibraryError('Unknown catalogue action')
        for kind in refs:
            self._kind(kind)
        with (nullcontext(_db) if _db is not None else self._database.connection()) as db:
            revision, ledger = self.check(db)
            state = empty()
            state.update(revision=revision, ledger=ledger)
            pending = [(kind, ident) for kind, ids in refs.items() for ident in ids]
            visited: set[tuple[str, str]] = set()
            while pending:
                kind, ident = pending.pop()
                if (kind, ident) in visited:
                    continue
                visited.add((kind, ident))
                row = db.execute(f'SELECT payload FROM {kind} WHERE id=%s', (ident,)).fetchone()
                if row is None:
                    continue  # Named new IDs are legal; mutation validates mandatory edges.
                record = self.restore(row[0], media=media)
                state[kind][ident] = record
                for name, (source, target) in RELATIONS.items():
                    if source == kind:
                        pending.extend((target, value[0]) for value in
                                       db.execute(f'SELECT target FROM {name} WHERE source=%s', (ident,)))
                    # Ownership decisions require incoming claims too. Provenance
                    # operations need all linked members, never unrelated payloads.
                    if target == kind and (action in ('identify', 'identify_work', 'select', 'merge_work', 'separate_edition',
                                                      'resolve', 'admit', 'publish', 'retire') or name == 'asset_origin'):
                        pending.extend((source, value[0]) for value in
                                       db.execute(f'SELECT source FROM {name} WHERE target=%s', (ident,)))
                if kind in ('assets', 'editions') and action in ('resolve', 'publish', 'admit', 'identify_work'):
                    clause = 'target=%s' if kind == 'assets' else 'id IN (SELECT decision FROM decision_editions WHERE edition=%s)'
                    for (payload,) in db.execute('SELECT payload FROM ledger_records WHERE ' + clause, (ident,)):
                        decision = decode(payload)
                        pending.append(('assets', decision['target']))
                        pending.extend(('editions', eid) for eid in decision.get('affected_editions', []))
                if kind == 'assets' and record.get('identity'):
                    identity = record['identity']
                    pending.extend((table, identity[key]) for table, key in
                                   (('works', 'work'), ('editions', 'edition'), ('variants', 'variant')))
                if kind in ('works', 'editions') and action in ('identify', 'resolve', 'merge_work', 'separate_edition'):
                    column = 'work_id' if kind == 'works' else 'edition_id'
                    pending.extend(('assets', row[0]) for row in
                                   db.execute(f'SELECT id FROM assets WHERE {column}=%s', (ident,)))
            return state

    @staticmethod
    def _kind(kind: str) -> None:
        if kind not in TABLES:
            raise LibraryError('Unknown catalogue entity kind')

    def export(self) -> dict[str, Any]:
        """Explicit audit/recovery operation. Ordinary callers use get/page/commit."""
        with self._database.connection() as db:
            revision, ledger = self.check(db)
            state = empty()
            state.update(revision=revision, ledger=ledger)
            for kind in TABLES:
                state[kind] = {ident: self.restore(payload) for ident, payload in
                               db.execute(f'SELECT id,payload FROM {kind} ORDER BY id')}
            validate(state)
            return state

    def semantic_digest(self) -> str:
        result = hashlib.sha256()
        def add(value: Any) -> None:
            raw = encode(value).encode()
            result.update(len(raw).to_bytes(8, 'big')); result.update(raw)
        with self._database.connection() as db:
            revision, ledger = self.check(db)
            add({'schema': 1, 'revision': revision, 'ledger': ledger})
            for kind in TABLES:
                add(kind)
                for ident, payload in db.execute(f'SELECT id,payload FROM {kind} ORDER BY id'):
                    add([ident, self.restore(payload)])
        return result.hexdigest()

    def audit(self) -> None:
        with self._database.connection() as db:
            self.check(db)
            db.execute('SET CONSTRAINTS ALL IMMEDIATE')
            # Materialize one repeatable-read snapshot within the DB budget.
            # Local decoding, attachment I/O and validation must not hold it open.
            rows = {kind: db.execute(f'SELECT id,payload FROM {kind} ORDER BY id').fetchall()
                    for kind in TABLES}
            relations: dict[str, dict[str, set[str]]] = {}
            for name in RELATIONS:
                members: dict[str, set[str]] = {}
                for source, target in db.execute(f'SELECT source,target FROM {name}'):
                    members.setdefault(source, set()).add(target)
                relations[name] = members
        linked = {kind: {ident: self.restore(payload, media=False) for ident, payload in rows[kind]}
                  for kind in ('works', 'editions', 'variants', 'origins')}
        for kind in TABLES:
            for ident, payload in rows[kind]:
                record = self.restore(payload)
                self._validate_record(kind, ident, record)
                self._validate_links(None, kind, ident, record, linked=linked)
                expected = self._edges(kind, record)
                for name, (source, _) in RELATIONS.items():
                    if source == kind:
                        if relations[name].get(ident, set()) != set(expected.get(name, [])):
                            raise LibraryError('Catalogue relation index differs from record')

    def backup(self, destination: Path) -> Store:
        raise LibraryError('SQLite restore is retired; recover current PostgreSQL state forward')

    def _register(self, key: str, value: str) -> None:
        with self._database.connection(write=True) as db:
            db.execute("INSERT INTO commit_receipts(key,digest,state) VALUES (%s,%s,'registered') ON CONFLICT DO NOTHING", (key, value))
            row = db.execute('SELECT digest,state FROM commit_receipts WHERE key=%s', (key,)).fetchone()
            if row is None or row[0] != value:
                raise LibraryError('Commit key input mismatch')
            if row[1] == 'aborted':
                raise LibraryError('Commit key was fenced aborted')

    def outcome(self, key: str, *, input_digest: str) -> dict[str, Any]:
        """Fence delayed originals before reporting an absent commit aborted."""
        try:
            with self._database.connection(write=True) as db:
                db.execute("INSERT INTO commit_receipts(key,digest,state) VALUES (%s,%s,'aborted') ON CONFLICT DO NOTHING", (key, input_digest))
                row = db.execute('SELECT digest,state,receipt FROM commit_receipts WHERE key=%s FOR UPDATE', (key,)).fetchone()
                if row is None or row[0] != input_digest:
                    raise LibraryError('Commit key input mismatch')
                if row[1] == 'committed':
                    return {'state': 'committed', 'receipt': decode(row[2])}
                db.execute("UPDATE commit_receipts SET state='aborted' WHERE key=%s", (key,))
                return {'state': 'aborted'}
        except (CommitUnknown, psycopg.OperationalError):
            return {'state': 'unresolved'}
        except LibraryError as exc:
            if str(exc) == 'storage_busy':
                return {'state': 'unresolved'}
            raise

    @staticmethod
    def _guard_keys(state: dict[str, Any], claims: dict[str, list[str]] | None = None) -> set[str]:
        keys: set[str] = set()
        for kind in TABLES:
            for ident, record in state.get(kind, {}).items():
                keys.add('entity:'+kind+':'+ident)
                if kind in ('assets', 'editions'):
                    keys.add('decisions:'+kind+':'+ident)
                for name, (source, target) in RELATIONS.items():
                    if source == kind:
                        keys.add('out:'+name+':'+ident)
                    if target == kind:
                        keys.add('in:'+name+':'+ident)
                for name, targets in Store._edges(kind, record).items():
                    keys.update('in:'+name+':'+target for target in targets)
                if kind == 'editions':
                    keys.update('claim:'+field+':'+str(record[field]) for field in ('isbn','reference') if record.get(field))
                source = record.get('source')
                if source:
                    keys.add('acquisition:'+str(source))
                if kind == 'origins':
                    if type(record.get('pid')) is int and type(record.get('fid')) is int:
                        keys.add('native-file:'+str(record['pid'])+':'+str(record['fid']))
                    if record.get('sha256'):
                        keys.add('entity:assets:'+record['sha256'])
                for field in ('target','destination'):
                    if record.get(field):
                        keys.add('destination:'+str(record[field]))
                for field in ('publication','admission','provisional'):
                    selected = record.get(field) or {}
                    if isinstance(selected, dict) and selected.get('target'):
                        keys.add('destination:'+str(selected['target']))
        for field, values in (claims or {}).items():
            keys.update('claim:'+field+':'+value for value in values)
        return keys

    @staticmethod
    def transition_digest(change: Change, *, expected_revision: int,
                          conflict: dict[str, Any] | None = None,
                          obligations: list[Obligation] | None = None, now: datetime | None = None,
                          attempt: tuple[str, int] | None = None, outcome: str = 'done',
                          due_at: datetime | None = None, proof: str = '',
                          source_progress: SourceProgress | None = None,
                          completions: list[OperationalCompletion] | None = None, reconciled: bool=False) -> str:
        """Retain this identity before attempting a transition or resolving its receipt."""
        from dataclasses import asdict
        request = {'change': asdict(change), 'conflict': conflict,
                   'expected': expected_revision if conflict is None else None,
                   'attempt': list(attempt) if attempt else None,
                   'obligations': [asdict(value) for value in obligations or []],
                   'now': now, 'outcome': outcome, 'due': due_at, 'proof': proof,
                   'source_progress': asdict(source_progress) if source_progress else None,
                   'completions':[asdict(value) for value in completions or []]}
        if reconciled:request['reconciled']=True
        return hashlib.sha256(encode(request).encode()).hexdigest()

    def commit(self, change: Change, *, expected_revision: int, conflict: dict[str, Any] | None = None,
               obligations: list[Obligation] | None = None, now: datetime | None = None,
               attempt: tuple[str, int] | None = None, commit_key: str | None = None,
               outcome: str = 'done', due_at: datetime | None = None, proof: str = '',
               source_progress: SourceProgress | None = None,
               completions: list[OperationalCompletion] | None = None, reconciled: bool=False) -> int:
        from library.locking import acquisition_deadline, bounded_acquisition
        import time
        change, conflict = copy.deepcopy(change), copy.deepcopy(conflict)
        obligations, source_progress = copy.deepcopy(obligations), copy.deepcopy(source_progress)
        completions=copy.deepcopy(completions or [])
        value = self.transition_digest(change, expected_revision=expected_revision, conflict=conflict,
            obligations=obligations, now=now, attempt=attempt, outcome=outcome, due_at=due_at, proof=proof,
            source_progress=source_progress,completions=completions,reconciled=reconciled)
        key = commit_key or 'result:'+value
        prepared = {}
        for kind, records in change.upserts.items():
            self._kind(kind)
            for ident, record in records.items():
                self._validate_record(kind, ident, record)
                prepared[kind, ident] = (record, *self.prepare(record))
        for kind in change.deletes:
            self._kind(kind)
        proposed = {kind: dict(change.upserts.get(kind, {})) for kind in TABLES}
        for kind, ids in change.deletes.items():
            for ident in ids:
                proposed[kind].setdefault(ident, {})
        stop = min(acquisition_deadline.get() or float('inf'), time.monotonic()+10)
        keys = self._guard_keys(proposed, (conflict or {}).get('claims'))
        if conflict:
            for kind, ids in conflict['entities'].items():
                for ident in ids:
                    proposed[kind].setdefault(ident, {})
            for kind, ids in conflict['refs'].items():
                for ident in ids:
                    proposed[kind].setdefault(ident, {})
            keys.update(self._guard_keys(proposed, conflict['claims']))
        if attempt:
            keys.add('task:'+attempt[0])
            from library.stage_tasks import StageTasks
            task = StageTasks(self).get(attempt[0])
            if task:
                keys.update(('queue:'+task.stage,'task-entity:'+task.stage+':'+task.entity))
                if task.source:
                    keys.add('source:'+task.source)
                    with self._database.connection() as source_db:
                        keys.update(StageTasks._source_guards(source_db,task))
        from library.operational_state import completion_guards
        for completion in completions:
            keys.update(completion_guards(completion))
        if source_progress:
            from library.operational_state import completion_guards
            for completion in source_progress.completions:
                keys.update(completion_guards(completion))
        if obligations:
            for obligation in obligations:
                keys.update(('queue:'+obligation.stage,'task:'+obligation.key, 'task-entity:'+obligation.stage+':'+obligation.entity))
                if obligation.source:
                    keys.add('source:'+obligation.source)
        if change.ledger is not None:
            keys.add('decision-chain')
        from library.locking import mutation_guard
        from library.stage_execution import effect_scopes, change_scopes
        scope = mutation_guard.get()
        with bounded_acquisition(stop), (scope(change) if scope is not None else
                                        effect_scopes(self.directory,change_scopes(change))):
            if key.startswith('decision-project:'):
                from dataclasses import asdict
                active=_projection.get()
                if active is None or active[1]!=encode(asdict(change)):
                    raise LibraryError('Decision projection capability required')
            self._register(key, value)
            while True:
                expanded: set[str] = set()
                with self._database.connection(write=True) as db:
                    row = db.execute('SELECT digest,state,receipt FROM commit_receipts WHERE key=%s FOR UPDATE', (key,)).fetchone()
                    if row is None or row[0] != value:
                        raise LibraryError('Commit key input mismatch')
                    if row[1] == 'committed':
                        return int(decode(row[2])['revision'])
                    if row[1] != 'registered':
                        raise LibraryError('Commit key was fenced aborted')
                    guards(db, keys)
                    current: dict[str, dict[str, Any]] = {kind: {} for kind in TABLES}
                    for kind, records in proposed.items():
                        for ident in records:
                            found = db.execute(f'SELECT payload FROM {kind} WHERE id=%s', (ident,)).fetchone()
                            current[kind][ident] = self.restore(found[0], media=False) if found else {}
                    expanded = self._guard_keys(current, (conflict or {}).get('claims'))-keys
                    if expanded:
                        db.execute('ROLLBACK')
                    else:
                        revision, ledger = self.check(db)
                        projection = None
                        if change.ledger is not None:
                            proposed_ledger = change.ledger
                            if proposed_ledger[:len(ledger)] != ledger:
                                raise LibraryError('Applied ledger must extend immutable prefix')
                            suffix = [r[0] for r in db.execute('SELECT id FROM ledger_records WHERE sequence>%s AND sequence<=%s ORDER BY sequence', (len(ledger),len(proposed_ledger)))]
                            if proposed_ledger[len(ledger):] != suffix:
                                raise LibraryError('Applied ledger is not a retained indexed prefix')
                            if len(suffix) == 1 and key == 'decision-project:'+suffix[0]:
                                from dataclasses import asdict
                                active=_projection.get()
                                indexed=db.execute('SELECT payload FROM ledger_records WHERE id=%s AND sequence=%s',
                                    (suffix[0],len(ledger)+1)).fetchone()
                                if (active is None or indexed is None or active!=(encode(decode(indexed[0])),encode(asdict(change)))
                                        or decode(indexed[0]).get('completion_task')):
                                    raise LibraryError('Decision projection capability required')
                                projection = suffix[0]
                            elif len(suffix)==1 and attempt is not None and conflict is not None:
                                if self._completion_projection(db,suffix[0],attempt,conflict):projection=suffix[0]
                        self._check_decision_reservations(db,keys,projection=projection)
                        from library.locking import commit_guard
                        acting = commit_guard.get()
                        if acting is not None:
                            acting(db)
                        if conflict is not None:
                            from library.conflicts import verify
                            verify(self,db,conflict,change,projection=projection,completion_attempt=attempt)
                        elif change.upserts or change.deletes or change.ledger is not None:
                            # Unscoped compatibility saves validate the exact read snapshot.
                            if revision != expected_revision:
                                raise LibraryError('Stale catalogue revision')
                        from library.stage_tasks import StageTasks
                        tasks = StageTasks(self)
                        from library.operational_state import apply_completion
                        for completion in completions:
                            apply_completion(db,completion)
                        if attempt:
                            if now is None:
                                raise LibraryError('Atomic completion requires time')
                            tasks._finish(db,*attempt,now=now,outcome=outcome,due_at=due_at,proof=proof,
                                          source_progress=source_progress,reconciled=reconciled)
                        db.execute('SET CONSTRAINTS ALL DEFERRED')
                        for (kind, ident), (record, payload, attachments) in prepared.items():
                            for aid, length in attachments.items():
                                db.execute('INSERT INTO attachments VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', (aid,'utf8-legacy-cover',length,'payloads/'+aid))
                            columns = ('revision','payload',*QUERY_COLUMNS)
                            db.execute(f'INSERT INTO {kind} VALUES (' + ','.join(['%s']*(3+len(QUERY_COLUMNS))) + ') ON CONFLICT(id) DO UPDATE SET ' + ','.join(c+'=excluded.'+c for c in columns), (ident,revision+1,payload,*query_values(kind,record)))
                        for kind, ids in change.deletes.items():
                            for ident in ids:
                                self._relations(db,kind,ident,{})
                                db.execute(f'DELETE FROM {kind} WHERE id=%s',(ident,))
                        for (kind,ident),(record,_,_) in prepared.items():
                            self._relations(db,kind,ident,record)
                            self._validate_links(db,kind,ident,record)
                        for kind,ident in prepared:
                            for name,(source,target) in RELATIONS.items():
                                if target == kind:
                                    for owner,payload in db.execute(f'SELECT e.id,e.payload FROM {source} e JOIN {name} r ON e.id=r.source WHERE r.target=%s',(ident,)):
                                        self._validate_links(db,source,owner,self.restore(payload,media=False))
                        if change.ledger is not None:
                            db.execute('UPDATE meta SET ledger=%s WHERE singleton=1',(encode(change.ledger),))
                            for applied in change.ledger[len(ledger):]:
                                db.execute("UPDATE decision_reservations SET status='projected' WHERE id=%s",(applied,))
                        self._refresh_rows(db,{kind:set(change.upserts.get(kind,{}))|set(change.deletes.get(kind,[])) for kind in TABLES})
                        for obligation in obligations or []:
                            if now is None:
                                raise LibraryError('Obligations require time')
                            tasks._discover(db,obligation,now=now)
                        changed = bool(change.upserts or change.deletes or change.ledger is not None)
                        receipt = {'revision':revision+int(changed), 'attempt':list(attempt) if attempt else None}
                        db.execute("UPDATE commit_receipts SET state='committed',catalogue=%s,receipt=%s WHERE key=%s",(changed,encode(receipt),key))
                        return revision+int(changed)
                keys.update(expanded)

    def _validate_links(self, db: DeadlineConnection | None, kind: str, ident: str, record: dict[str, Any], *,
                        linked: dict[str, dict[str, dict[str, Any]]] | None = None) -> None:
        def get(table: str, key: str) -> dict[str, Any] | None:
            if linked is not None:
                return linked[table].get(key)
            assert db is not None, 'Uncached link validation requires a connection'
            row = db.execute(f'SELECT payload FROM {table} WHERE id=%s', (key,)).fetchone()
            return self.restore(row[0], media=False) if row else None
        if kind == 'assets' and record.get('identity'):
            identity = record['identity']
            work, edition, variant = (get(table, identity[key]) for table, key in
                                      (('works', 'work'), ('editions', 'edition'), ('variants', 'variant')))
            if not work or not edition or not variant or edition['work'] != identity['work'] or variant != {
                    **variant, 'edition': identity['edition'], 'asset': ident}:
                raise LibraryError('Asset identity references missing or conflicting entity')
        if kind == 'editions' and record.get('selected'):
            variant = get('variants', record['selected'])
            if not variant or variant['edition'] != ident:
                raise LibraryError('Selected variant belongs to a different edition')
        if kind == 'origins' and record.get('parent_origin'):
            parent = get('origins', record['parent_origin'])
            if (not parent or record.get('parent_sha256') != parent.get('sha256')
                    or record.get('member_sha256') != record['sha256']
                    or not isinstance(record.get('member'), str)
                    or not record['path'].startswith('bundle-inputs/' + str(parent['sha256']) + '/')):
                raise LibraryError('Invalid bundle child provenance')

    @staticmethod
    def _relations(db: DeadlineConnection, kind: str, ident: str, record: dict[str, Any]) -> None:
        if kind == 'assets':
            db.execute('DELETE FROM asset_flags WHERE asset=%s', (ident,))
            if record:
                db.execute('INSERT INTO asset_flags VALUES (%s,%s)',
                           (ident, int(record.get('provisional', {}).get('status') == 'published')))
        if kind in ('assets', 'origins'):
            from library.primitives import digest
            label = 'asset' if kind == 'assets' else 'origin'
            review_kind = 'asset_review' if kind == 'assets' else 'file_review'
            db.execute('DELETE FROM case_bindings WHERE kind=%s AND target=%s', (kind, ident))
            if record:
                db.execute('INSERT INTO case_bindings VALUES (%s,%s,%s)',
                           (digest({label: ident, 'kind': review_kind}), kind, ident))
        if kind == 'editions':
            db.execute('DELETE FROM edition_claims WHERE edition=%s', (ident,))
            for key in ('isbn', 'reference'):
                if record.get(key):
                    db.execute('INSERT INTO edition_claims VALUES (%s,%s,%s)', (key, str(record[key]), ident))
        edges = Store._edges(kind, record)
        for name, targets in edges.items():
            db.execute(f'DELETE FROM {name} WHERE source=%s', (ident,))
            db.cursor().executemany(f'INSERT INTO {name} VALUES (%s,%s)', [(ident, target) for target in targets])

    @staticmethod
    def _edges(kind: str, record: dict[str, Any]) -> dict[str, list[str]]:
        edges: dict[str, list[str]] = {}
        if kind == 'editions':
            edges = {'edition_work': [record['work']] if record.get('work') else [],
                     'selected_variant': [record['selected']] if record.get('selected') else [],
                     'edition_publication_asset': [record['publication']['asset']] if record.get('publication', {}).get('asset') else []}
        elif kind == 'variants':
            edges = {name: [record[field]] if record.get(field) else [] for name, field in
                     (('variant_edition', 'edition'), ('variant_asset', 'asset'))}
        elif kind == 'assets':
            edges = {'asset_origin': record.get('origins', [])}
            identity = record.get('identity') or {}
            edges.update({name: [identity[key]] if identity.get(key) else [] for name, key in
                          (('identity_work', 'work'), ('identity_edition', 'edition'), ('identity_variant', 'variant'))})
        elif kind == 'origins':
            edges = {'origin_parent': [record['parent_origin']] if record.get('parent_origin') else []}
        elif kind == 'operations':
            edges = {'operation_edition': [record['edition']] if record.get('edition') else []}
        elif kind == 'artifacts':
            edges = {'artifact_asset': record.get('source_assets', []),
                     'artifact_edition': record.get('editions', [])}
        return edges

    @staticmethod
    def _validate_record(kind: str, ident: str, record: dict[str, Any]) -> None:
        if not isinstance(ident, str) or not isinstance(record, dict):
            raise LibraryError('Invalid entity')
        requirements = {'works': ('title',), 'editions': ('work', 'language', 'publisher', 'evidence'),
                        'variants': ('edition', 'asset'), 'assets': ('sha256', 'size', 'format', 'origins'),
                        'origins': ('source', 'pid', 'fid', 'path', 'sha256')}
        if any(key not in record for key in requirements.get(kind, ())):
            raise LibraryError('Missing entity fields')
        if kind == 'assets':
            state = empty()
            # Reuse all legacy local asset gates with a minimal relation closure.
            state['assets'][ident] = record
            state['origins'] = {oid: {'source': '', 'pid': 0, 'fid': 0, 'path': '', 'sha256': None}
                                for oid in record['origins']}
            identity = record.get('identity')
            if identity:
                state['works'][identity['work']] = {'title': 'closure'}
                state['editions'][identity['edition']] = {'work': identity['work'], 'language': '', 'publisher': '', 'evidence': []}
                state['variants'][identity['variant']] = {'asset': ident, 'edition': identity['edition']}
            validate(state)
        if kind == 'origins':
            state = empty()
            # Parent equivalence is checked against actual rows by the caller's
            # closure audit; retain the legacy path/ID/hash checks here.
            state['origins'][ident] = {k: v for k, v in record.items() if k != 'parent_origin'}
            validate(state)
