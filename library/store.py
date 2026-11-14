"""Entity-oriented catalogue. Files and external effects stay outside transactions."""
from __future__ import annotations

from contextlib import nullcontext
import copy
from dataclasses import field
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from psycopg import sql
import tempfile
import time
from typing import Any, TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    from library.stage_tasks import Obligation
    from library.operational_state import OperationalCompletion

from library.primitives import LibraryError, TABLES, empty, sync_dir, validate
from library.codec import decode, encode
from library.postgres import Database, DeadlineConnection, guards


from library.stage_model import Change as Change, PreparedResult as PreparedResult, SourceProgress as SourceProgress


RELATIONS = {'asset_origin': ('assets', 'origins'), 'origin_parent': ('origins', 'origins')}

_TRACED_AT: dict[str, float] = {}


def _trace_full_read(what: str) -> None:
    """Name the caller of a full catalogue read, at most once a minute per process (profiling the whole-table reads)."""
    import time
    import traceback
    now = time.monotonic()
    if now - _TRACED_AT.get(what, -60.0) < 60:
        return
    _TRACED_AT[what] = now
    frames = [f'{f.filename.rsplit("/", 1)[-1]}:{f.name}:{f.lineno}' for f in traceback.extract_stack()[:-1]
              if '/library/' in f.filename or '/booksys_core/' in f.filename][-8:]
    import sys
    # A plain line, not a diagnostic event: the event schema is closed and carries no free text.
    sys.stderr.write(f'full_catalogue_read what={what} caller={">".join(frames)}\n')


QUERY_COLUMNS = ('source_ref', 'pid', 'fid', 'work_id', 'edition_id', 'asset_id', 'state', 'next_attempt', 'identity_status', 'technical_status', 'resolution_status', 'disposition', 'admission_status')
ACTIONS = ('identify', 'admit', 'publish', 'retire')


def query_values(kind: str, record: dict[str, Any]) -> tuple[Any, ...]:
    identity = record.get('identity') or {}
    return (record.get('source'), record.get('pid'), record.get('fid'),
            identity.get('work'),
            record.get('edition') or identity.get('edition'),
            record.get('asset') or (record.get('sha256') if kind == 'origins' else None),
            record.get('state') or record.get('availability'), record.get('automation', {}).get('next_attempt'),
            'contested' if record.get('contested') else 'identified' if
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
    for name, (source, target) in RELATIONS.items():
        db.execute(f'CREATE TABLE {name} (source TEXT COLLATE \"C\" NOT NULL REFERENCES {source}(id) '
                   'DEFERRABLE INITIALLY DEFERRED, target TEXT COLLATE \"C\" NOT NULL '
                   f'REFERENCES {target}(id) DEFERRABLE INITIALLY DEFERRED, PRIMARY KEY(source,target))')
        db.execute(f'CREATE INDEX {name}_target ON {name}(target,source)')
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
            db.execute('CREATE TABLE conflict_guards(key TEXT COLLATE \"C\" PRIMARY KEY)')
            from library.postgres import guard_schema
            guard_schema(db,database.schema)
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
        return store

    def verify(self, db: DeadlineConnection, *, importing: bool = False) -> tuple[int, list[str]]:
        """Authority, schema and ledger head. Returns (global_revision, ledger)."""
        row = db.execute('SELECT authority_id,schema_version,global_revision,ledger,ready,required_runtime FROM meta WHERE singleton=1').fetchone()
        if (not row or row[0] != self.authority or row[1] != 1 or row[4] not in ((0,1) if importing else (1,))
                or row[5] != 'postgresql>=18.6'):
            raise LibraryError('Catalogue authority or schema mismatch')
        ledger = decode(row[3])
        if not isinstance(ledger, list) or len(set(ledger)) != len(ledger):
            raise LibraryError('Invalid catalogue ledger head')
        return int(row[2]), ledger

    def check(self, db: DeadlineConnection, *, importing: bool = False) -> tuple[int, list[str]]:
        return self.verify(db, importing=importing)

    def _freshness(self, db: DeadlineConnection) -> str:
        return hashlib.sha256(encode([self.authority, self.verify(db)[0]]).encode()).hexdigest()

    @property
    def revision(self) -> int:
        with self._database.connection() as db:
            return self.check(db)[0]

    def polled_revision(self) -> int:
        """Change detection for pollers: up to one second old."""
        now = time.monotonic()
        seen = getattr(self, '_revision_seen', None)
        if seen is not None and now - seen[0] < 1.0:
            return int(seen[1])
        value = self.revision
        self._revision_seen = (now, value)
        return value

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
            self.verify(db)
            row = db.execute(f'SELECT payload FROM {kind} WHERE id=%s', (ident,)).fetchone()
            return self.restore(row[0], media=media) if row else None

    def ids(self, kind: str) -> list[str]:
        self._kind(kind)
        with self._database.connection() as db:
            self.verify(db)
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
            conditions = ['id>%s'] + [column + (' IS NULL' if filters[column] is None else '=%s') for column in sorted(filters)]
            rows = db.execute(f'SELECT id,payload FROM {kind} WHERE ' + ' AND '.join(conditions)
                              + ' ORDER BY id LIMIT %s',
                              (after, *(filters[key] for key in sorted(filters) if filters[key] is not None), limit + 1)).fetchall()
            items = [{'id': ident, 'record': self.restore(payload, media=media)} for ident, payload in rows[:limit]]
            next_cursor = base64.urlsafe_b64encode(json.dumps({'revision': freshness, 'shape': shape,
                'after': items[-1]['id']}).encode()).decode() if len(rows) > limit else None
            return {'revision': revision, 'items': items, 'cursor': next_cursor}

    def changed_ids(self, kind: str, since_revision: int) -> list[str]:
        """Ids written after a catalogue revision: a commit at revision R stamps its rows R+1. Index free, ids only."""
        self._kind(kind)
        with self._database.connection() as db:
            self.verify(db)
            return [row[0] for row in db.execute(f'SELECT id FROM {kind} WHERE revision > %s ORDER BY id', (since_revision,))]

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
        conditions = ['id>%s'] + [column + (' IS NULL' if filters[column] is None else '=%s') for column in sorted(filters)]
        with self._database.connection() as db:
            self.verify(db)
            rows = db.execute(f'SELECT id,payload FROM {kind} WHERE ' + ' AND '.join(conditions)
                + ' ORDER BY id LIMIT %s', (after, *(filters[key] for key in sorted(filters) if filters[key] is not None), limit)).fetchall()
            return [(ident, self.restore(payload, media=False)) for ident, payload in rows]

    def _refresh_rows(self, db: DeadlineConnection, changed: dict[str, set[str]]) -> None:
        from library.projection import operator_row
        affected = set(changed.get('origins', set()))
        assets = set(changed.get('assets', set()))
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
            value = operator_row(oid, origin, asset, {})
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
                    _db: DeadlineConnection | None = None,
                    _payloads: dict[str, dict[str, tuple[str, dict[str, Any]]]] | None = None) -> dict[str, Any]:
        """Resolve relation closure in a consistent snapshot, with exact legacy media.

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
                if _payloads is not None and not media:
                    # Conflict snapshots reuse this exact envelope and raw payload.
                    envelope = decode(row[0])
                    _payloads.setdefault(kind, {})[ident] = (row[0], envelope)
                    record = envelope['record']
                else:
                    record = self.restore(row[0], media=media)
                state[kind][ident] = record
                for name, (source, target) in RELATIONS.items():
                    if source == kind:
                        pending.extend((target, value[0]) for value in
                                       db.execute(f'SELECT target FROM {name} WHERE source=%s', (ident,)))
                    if target == kind and (action in ('identify', 'admit', 'publish', 'retire') or name == 'asset_origin'):
                        pending.extend((source, value[0]) for value in
                                       db.execute(f'SELECT source FROM {name} WHERE target=%s', (ident,)))
            return state

    @staticmethod
    def _kind(kind: str) -> None:
        if kind not in TABLES:
            raise LibraryError('Unknown catalogue entity kind')

    def export(self) -> dict[str, Any]:
        """Explicit audit/recovery operation. Ordinary callers use get/page/commit."""
        _trace_full_read('export')
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
            self.verify(db)
            db.execute('SET CONSTRAINTS ALL IMMEDIATE')
            # Materialize one repeatable-read snapshot within the DB budget.
            # Local decoding, attachment I/O and validation must not hold it open.
            rows: dict[str, list[tuple[str, str]]] = {}
            for kind in TABLES:
                # Bound the driver's result buffer, without decoding inside the snapshot.
                with db.cursor(name='startup_audit') as cursor:
                    cursor.itersize = 128
                    cursor.execute(f'SELECT id,payload FROM {kind} ORDER BY id')
                    rows[kind] = list(cursor)
            relations: dict[str, dict[str, set[str]]] = {}
            for name in RELATIONS:
                members: dict[str, set[str]] = {}
                for source, target in db.execute(f'SELECT source,target FROM {name}'):
                    members.setdefault(source, set()).add(target)
                relations[name] = members
        # Retain raw parent payloads only; most origins need no parent lookup.
        linked = {'origins': dict(rows['origins'])}
        for kind in TABLES:
            for ident, payload in rows.pop(kind):
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

    @staticmethod
    def _guard_keys(state: dict[str, Any], claims: dict[str, list[str]] | None = None) -> set[str]:
        keys: set[str] = set()
        for kind in TABLES:
            for ident, record in state.get(kind, {}).items():
                keys.add('entity:'+kind+':'+ident)
                for name, (source, target) in RELATIONS.items():
                    if source == kind:
                        keys.add('out:'+name+':'+ident)
                    if target == kind:
                        keys.add('in:'+name+':'+ident)
                for name, targets in Store._edges(kind, record).items():
                    keys.update('in:'+name+':'+target for target in targets)
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

    def commit(self, change: Change, *, expected_revision: int, conflict: dict[str, Any] | None = None,
               obligations: list[Obligation] | None = None, now: datetime | None = None,
               outcome: str = 'done', due_at: datetime | None = None, proof: str = '',
               source_progress: SourceProgress | None = None,
               completions: list[OperationalCompletion] | None = None, reconciled: bool=False,
               work_claim: tuple[Any, Any] | None = None) -> int:
        """work_claim is (WorkItems, Item): the end of that claim and the successors join this transaction."""
        from library.locking import acquisition_deadline, bounded_acquisition
        import time
        change, conflict = copy.deepcopy(change), copy.deepcopy(conflict)
        obligations, source_progress = copy.deepcopy(obligations), copy.deepcopy(source_progress)
        completions=copy.deepcopy(completions or [])
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
        from library.locking import mutation_guard
        from library.stage_execution import effect_scopes, change_scopes
        scope = mutation_guard.get()
        with bounded_acquisition(stop), (scope(change) if scope is not None else
                                        effect_scopes(self.directory,change_scopes(change))):
            while True:
                expanded: set[str] = set()
                with self._database.connection(write=True) as db:
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
                        db.execute('SELECT singleton FROM meta WHERE singleton=1 FOR UPDATE')
                        revision, ledger = self.check(db)
                        from library.locking import commit_guard
                        acting = commit_guard.get()
                        if acting is not None:
                            acting(db)
                        if conflict is not None:
                            from library.conflicts import verify
                            verify(self,db,conflict,change)
                        elif change.upserts or change.deletes:
                            # Unscoped compatibility saves validate the exact read snapshot.
                            if revision != expected_revision:
                                raise LibraryError('Stale catalogue revision')
                        from library.operational_state import apply_completion
                        for completion in completions:
                            apply_completion(db,completion)
                        if work_claim is not None:
                            if now is None:
                                raise LibraryError('Atomic completion requires time')
                            self._finish_work(db, work_claim, now=now, outcome=outcome, due_at=due_at, proof=proof,
                                              source_progress=source_progress)
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
                        self._refresh_rows(db,{kind:set(change.upserts.get(kind,{}))|set(change.deletes.get(kind,[])) for kind in TABLES})
                        for obligation in obligations or []:
                            if now is None:
                                raise LibraryError('Obligations require time')
                            from library.stage_tasks import StageTasks
                            StageTasks(self)._discover(db,obligation,now=now)
                        changed = bool(change.upserts or change.deletes)
                        if changed:
                            db.execute('UPDATE meta SET global_revision=global_revision+1 WHERE singleton=1')
                        return revision+int(changed)
                keys.update(expanded)

    def _finish_work(self, db: DeadlineConnection, work_claim: tuple[Any, Any], *, now: datetime, outcome: str,
                     due_at: datetime | None, proof: str, source_progress: SourceProgress | None) -> None:
        """End a work_items claim inside the commit transaction (same rules as StageTasks._finish)."""
        from datetime import timedelta
        work, claimed = work_claim
        if outcome not in ('done', 'retry', 'uncertain', 'defer'):
            raise LibraryError('Unknown task outcome')
        if claimed.source and outcome == 'done':
            from library.codec import encode
            from library.operational_state import apply_completion
            if (source_progress is None or (source_progress.source, source_progress.entity, source_progress.input_revision,
                                            source_progress.method) != (claimed.source, claimed.item, claimed.input_rev, claimed.method)):
                raise LibraryError('Source completion requires exact canonical progress')
            binding = db.execute('SELECT input_revision,method,guard FROM source_inputs WHERE source=%s AND entity=%s',
                                 (claimed.source, claimed.item)).fetchone()
            if binding is None or tuple(binding) != (claimed.input_rev, claimed.method, source_progress.guard):
                raise LibraryError('source_input_missing_or_changed')
            for completion in source_progress.completions:
                apply_completion(db, completion)
            db.execute('UPDATE source_inputs SET progress=%s WHERE source=%s AND entity=%s',
                       (encode(source_progress.result), claimed.source, claimed.item))
        if outcome == 'done':
            done = work.finish_in(db, claimed, 'done', detail=proof[:100] or 'done', now=now)
        elif outcome == 'defer':
            done = work.finish_in(db, claimed, 'waiting', detail='defer', now=now, reason='deferred',
                                  next_at=due_at or now + timedelta(seconds=30), refund=True)
        else:   # 'retry' and 'uncertain' retry alike: a step reads the external system again before it acts
            done = work.retry_in(db, claimed, proof[:100] or outcome, timedelta(seconds=300 if claimed.attempts == 1 else 900), now=now)
        if not done:
            raise LibraryError('Stale or inactive task result')

    def _validate_links(self, db: DeadlineConnection | None, kind: str, ident: str, record: dict[str, Any], *,
                        linked: dict[str, dict[str, str]] | None = None) -> None:
        def get(table: str, key: str) -> dict[str, Any] | None:
            if linked is not None:
                payload = linked[table].get(key)
                return self.restore(payload, media=False) if payload is not None else None
            assert db is not None, 'Uncached link validation requires a connection'
            row = db.execute(f'SELECT payload FROM {table} WHERE id=%s', (key,)).fetchone()
            return self.restore(row[0], media=False) if row else None
        if kind == 'origins' and record.get('parent_origin'):
            parent = get('origins', record['parent_origin'])
            if (not parent or record.get('parent_sha256') != parent.get('sha256')
                    or record.get('member_sha256') != record['sha256']
                    or not isinstance(record.get('member'), str)
                    or not record['path'].startswith('bundle-inputs/' + str(parent['sha256']) + '/')):
                raise LibraryError('Invalid bundle child provenance')

    @staticmethod
    def _relations(db: DeadlineConnection, kind: str, ident: str, record: dict[str, Any]) -> None:
        edges = Store._edges(kind, record)
        for name, targets in edges.items():
            db.execute(f'DELETE FROM {name} WHERE source=%s', (ident,))
            db.cursor().executemany(f'INSERT INTO {name} VALUES (%s,%s)', [(ident, target) for target in targets])

    @staticmethod
    def _edges(kind: str, record: dict[str, Any]) -> dict[str, list[str]]:
        edges: dict[str, list[str]] = {}
        if kind == 'assets':
            edges = {'asset_origin': record.get('origins', [])}
        elif kind == 'origins':
            edges = {'origin_parent': [record['parent_origin']] if record.get('parent_origin') else []}
        return edges

    @staticmethod
    def _validate_record(kind: str, ident: str, record: dict[str, Any]) -> None:
        if not isinstance(ident, str) or not isinstance(record, dict):
            raise LibraryError('Invalid entity')
        requirements = {'assets': ('sha256', 'size', 'format', 'origins'),
                        'origins': ('source', 'pid', 'fid', 'path', 'sha256')}
        if any(key not in record for key in requirements.get(kind, ())):
            raise LibraryError('Missing entity fields')
        if kind == 'assets':
            state = empty()
            # Reuse all legacy local asset gates with a minimal relation closure.
            state['assets'][ident] = record
            state['origins'] = {oid: {'source': '', 'pid': 0, 'fid': 0, 'path': '', 'sha256': None}
                                for oid in record['origins']}
            validate(state)
        if kind == 'origins':
            state = empty()
            # Parent equivalence is checked against actual rows by the caller's
            # closure audit; retain the legacy path/ID/hash checks here.
            state['origins'][ident] = {k: v for k, v in record.items() if k != 'parent_origin'}
            validate(state)
