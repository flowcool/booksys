"""Fixed operational field contracts. Domain owners call these inside their transactions.

Payload bytes remain the compatibility/hash surface. The closed field maps below
are not an extension store: unknown keys remain evidence in their existing owner.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import math
import re
from typing import Any

from library.codec import encode
from library.postgres import DeadlineConnection
from library.primitives import LibraryError

# column, decoded path, PostgreSQL type. Nullable fields preserve their exact
# missing/null/container shape in the unchanged compatibility document.
T = 'TEXT'
B = 'BIGINT'
F = 'DOUBLE PRECISION'
Z = 'TIMESTAMPTZ'
BOOL = 'BOOLEAN'


def fields(names: str, kind: str = T) -> list[tuple[str, str, str]]:
    return [(name, name, kind) for name in names.split()]


PACKAGE = fields('name folder mirror_source') + fields('dest mirror_original_pid', B)
LINK = fields('name url input_folder') + fields('fid status size', B)
FIELDS: dict[str, list[tuple[str, str, str]]] = {
    'assets': fields('format') + [('work_result_status', 'work_result.status', T)]
        + fields('size pages', B) + fields('contested', BOOL),
    'origins': fields('availability reason path reader_policy validation_policy decoder_readiness bundle_engine_policy '
        'parent_origin member parent_sha256 member_sha256 source_set_sha256')
        + fields('intake_attempts bundle_attempts size mtime_ns', B)
        + fields('intake_next_attempt bundle_next_attempt', F),
    'source_registry': fields('queue path kind cursor sha256') + [('native_queue', 'native.queue', T),
        ('last_scan', 'last_scan', Z), ('last_scan_order', 'last_scan', T)],
    'source_inputs': [(name, name, T) for name in ('kind', 'identity', 'original', 'registration_kind')],
    'collection_categories': fields('deep_page', B) + fields('last_cycle', Z),
    'collection_submissions': fields('operation_id status name primary_host') + fields('package_id', B),
    'fiche_progress': fields('status signature fiche_relative cover_relative') + fields('retry_after', F),
    'collection_checkpoints': fields('schema busy_used repeat_count', B)
        + fields('cycle_id state request_ref phase wait_reason wait_origin last_invalid_ref')
        + fields('started_at budget_renews_at not_before', Z) + fields('review_block', BOOL)
        + [('remote_deadline_at', 'remote_deadline', Z), ('cleanup_at', 'cleanup_recovery.at', Z),
           ('cleanup_status', 'cleanup_recovery.status', T), ('cleanup_proof', 'cleanup_recovery.proof', T)],
    'routing_packages': fields('source name phase kind original_folder fallback_host attempt_name attempt_folder')
        + [('native_pid', 'pid', B)] + fields('original_destination original_order attempt_pid', B)
        + fields('created_at', Z),
    'retirement_acquisitions': fields('phase evidence_digest') + fields('release', BOOL)
        + fields('created_at confirmed_at', Z),
    'observed_packages': PACKAGE,
    'observed_links': LINK,
    'retired_packages': PACKAGE + [('native_pid', 'pid', B)],
    'retired_links': LINK,
    'copy_operations': fields('asset kind phase target signature status') + fields('schema catalogue_revision', B) + fields('created_at', Z)
        + [('previous_operation', 'previous.operation', T)],
    'provider_cache': fields('status') + fields('http_status', B) + fields('expires', F),
}
KEYS: dict[str, tuple[str, ...]] = {
    'assets': ('id',), 'origins': ('id',), 'source_registry': ('id',),
    'source_inputs': ('source', 'entity'), 'collection_categories': ('source',),
    'collection_submissions': ('source',), 'fiche_progress': ('source',),
    'collection_checkpoints': ('kind',), 'routing_packages': ('pid',),
    'retirement_acquisitions': ('source',), 'observed_packages': ('pid',),
    'observed_links': ('pid', 'position'), 'retired_packages': ('source', 'pid'),
    'retired_links': ('source', 'pid', 'position'), 'copy_operations': ('namespace', 'id'),
    'provider_cache': ('namespace', 'url'),
}


def at(record: dict[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split('.'):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise LibraryError(f'Operational container is not a map: {path}')
        value = value.get(part)
    return value


def values(table: str, record: dict[str, Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    for column, path, kind in FIELDS[table]:
        value = at(record, path)
        # These policy/readiness values are structured in some accepted writers.
        # Keep their full document and a typed equality digest, never stringify.
        if table == 'origins' and column.endswith(('policy', 'readiness')) and isinstance(value, (dict, list)):
            value = 'codec-sha256:' + hashlib.sha256(encode(value).encode()).hexdigest()
        if table == 'collection_checkpoints' and path == 'remote_deadline' and value == 'unrepresentable':
            value = None  # explicit remote_deadline_kind retains this blocking state
        if value is not None:
            valid = False
            if kind == T:
                valid = type(value) is str
            elif kind == B:
                valid = type(value) is int and -(2**63) <= value < 2**63
            elif kind == F:
                valid = type(value) in (int, float) and math.isfinite(value) and float(value) == value
            elif kind == BOOL:
                valid = type(value) is bool
            elif kind == Z:
                if type(value) is str:
                    try:
                        parsed = datetime.fromisoformat(value)
                        fraction=re.search(r'T\d{2}:\d{2}:\d{2}[.,](\d+)',value)
                        valid = parsed.tzinfo is not None and math.isfinite(parsed.timestamp()) and not (fraction and any(c!='0' for c in fraction[1][6:]))
                    except (ValueError, OverflowError):
                        pass
            if not valid:
                raise LibraryError(f'Incompatible operational value: {table}.{path} ({type(value).__name__})')
        result.append(value)
    return tuple(result)


def schema(db: DeadlineConnection) -> None:
    for table, columns in FIELDS.items():
        for column, _, kind in columns:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
    db.execute('ALTER TABLE copy_operations ADD COLUMN ownership_valid BOOLEAN')
    db.execute('ALTER TABLE collection_checkpoints ADD COLUMN remote_deadline_kind TEXT')
    for owner in ('asset', 'origin'):
        parent = 'assets' if owner == 'asset' else 'origins'
        db.execute(f'CREATE TABLE {owner}_destinations({owner} TEXT NOT NULL REFERENCES {parent}(id) '
            'ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,slot TEXT NOT NULL,target TEXT NOT NULL,'
            f'PRIMARY KEY({owner},slot))')
        db.execute(f'CREATE INDEX {owner}_destinations_target ON {owner}_destinations(target,{owner})')
    db.execute('CREATE TABLE asset_metadata(asset TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,'
        'evidence TEXT,evaluation TEXT,outcome TEXT,method TEXT,evaluated_at TIMESTAMPTZ,next_at TIMESTAMPTZ)')
    db.execute('CREATE TABLE asset_admission(asset TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,'
        'operation TEXT,status TEXT,signature TEXT,target TEXT)')
    db.execute('CREATE TABLE origin_fingerprint(origin TEXT PRIMARY KEY REFERENCES origins(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,'
        'device BIGINT,inode BIGINT,size BIGINT,mtime_ns BIGINT,ctime_ns BIGINT)')
    db.execute('CREATE TABLE submission_links(source TEXT NOT NULL REFERENCES collection_submissions(source) ON DELETE CASCADE,'
        'role TEXT NOT NULL,position BIGINT NOT NULL,url TEXT NOT NULL,PRIMARY KEY(source,role,position))')
    db.execute('CREATE TABLE routing_members(pid TEXT NOT NULL REFERENCES routing_packages(pid) ON DELETE CASCADE,'
        'role TEXT NOT NULL,position BIGINT NOT NULL,member_key TEXT,payload TEXT NOT NULL,PRIMARY KEY(pid,role,position))')
    db.execute('CREATE TABLE copy_operation_files(namespace TEXT NOT NULL,operation TEXT NOT NULL,name TEXT NOT NULL,digest TEXT NOT NULL,'
        'PRIMARY KEY(namespace,operation,name),FOREIGN KEY(namespace,operation) REFERENCES copy_operations(namespace,id) ON DELETE CASCADE)')
    db.execute('CREATE INDEX copy_operations_asset ON copy_operations(namespace,asset,id)')
    db.execute('CREATE INDEX routing_packages_source ON routing_packages(source,native_pid)')
    db.execute("DO $g$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='booksys_app') THEN "
        "GRANT SELECT,INSERT,UPDATE,DELETE ON asset_destinations,origin_destinations,asset_metadata,asset_admission,"
        "origin_fingerprint,submission_links,routing_members,copy_operation_files TO booksys_app; END IF; END $g$")


def sync(db: DeadlineConnection, table: str, key: tuple[Any, ...], record: dict[str, Any]) -> None:
    """Keep one closed domain row and its queried children in the owner's transaction."""
    columns = FIELDS[table]
    where = ' AND '.join(name + '=%s' for name in KEYS[table])
    db.execute(f'UPDATE {table} SET ' + ','.join(column + '=%s' for column, _, _ in columns)
        + ' WHERE ' + where, (*values(table, record), *key))
    if table == 'copy_operations':
        db.execute('UPDATE copy_operations SET ownership_valid=(copy_operation_asset_v1(payload) IS NOT NULL) WHERE namespace=%s AND id=%s',key)
    if table == 'collection_checkpoints':
        remote = record.get('remote_deadline')
        kind = 'none' if remote is None else 'unrepresentable' if remote == 'unrepresentable' else 'finite'
        db.execute('UPDATE collection_checkpoints SET remote_deadline_kind=%s WHERE kind=%s', (kind, key[0]))
    if table in ('assets', 'origins'):
        owner = 'asset' if table == 'assets' else 'origin'
        relation = owner + '_destinations'
        db.execute(f'DELETE FROM {relation} WHERE {owner}=%s', key)
        for slot in ('target', 'destination', 'publication.target', 'admission.target', 'provisional.target'):
            target = at(record, slot)
            if target is not None:
                if type(target) is not str:
                    raise LibraryError(f'Incompatible destination: {table}.{slot}')
                db.execute(f'INSERT INTO {relation} VALUES (%s,%s,%s)', (key[0], slot, target))
        if table == 'assets':
            for child, field, names, kinds in (
                ('asset_metadata', 'stage_metadata', 'evidence evaluation outcome method evaluated_at next_at', (T,T,T,T,Z,Z)),
                ('asset_admission', 'admission', 'operation status signature target', (T,T,T,T))):
                db.execute(f'DELETE FROM {child} WHERE asset=%s', key)
                value = record.get(field)
                if value is not None:
                    if not isinstance(value, dict):
                        raise LibraryError(f'Incompatible operational value: assets.{field}')
                    args = []
                    for name, kind in zip(names.split(), kinds):
                        val = value.get('at' if field=='stage_metadata' and name=='evaluated_at' else name)
                        if val is not None and (not isinstance(val,str) or kind == Z and datetime.fromisoformat(val).tzinfo is None):
                            raise LibraryError(f'Incompatible operational value: assets.{field}.{name}')
                        args.append(val)
                    db.execute(f'INSERT INTO {child} VALUES (' + ','.join(['%s']*(len(args)+1)) + ')', (*key, *args))
        else:
            db.execute('DELETE FROM origin_fingerprint WHERE origin=%s', key)
            fingerprint = record.get('fingerprint')
            if isinstance(fingerprint, list) and len(fingerprint) == 5 and all(type(v) is int and -(2**63) <= v < 2**63 for v in fingerprint):
                db.execute('INSERT INTO origin_fingerprint VALUES (%s,%s,%s,%s,%s,%s)', (*key, *fingerprint))
            elif fingerprint is not None and not isinstance(fingerprint, str):
                raise LibraryError('Incompatible operational value: origins.fingerprint')
    elif table == 'collection_submissions':
        db.execute('DELETE FROM submission_links WHERE source=%s', key)
        for role in ('links', 'all_links', 'primary_links'):
            links = record.get(role, [])
            if not isinstance(links, list) or any(type(link) is not str for link in links):
                raise LibraryError(f'Incompatible submission links: {role}')
            for position, url in enumerate(links):
                db.execute('INSERT INTO submission_links VALUES (%s,%s,%s,%s)', (*key, role, position, url))
    elif table == 'routing_packages':
        db.execute('DELETE FROM routing_members WHERE pid=%s', key)
        for role in ('primary_links', 'fallback_links', 'original_files', 'attempt_files', 'input_folders', 'files'):
            members = record.get(role, [])
            if not isinstance(members, (list, dict)):
                raise LibraryError(f'Incompatible routing members: {role}')
            entries = members.items() if isinstance(members, dict) else enumerate(members)
            for position, (member_key, value) in enumerate(entries):
                db.execute('INSERT INTO routing_members VALUES (%s,%s,%s,%s,%s)',
                    (*key, role, position, str(member_key) if isinstance(members,dict) else None, encode(value)))
    elif table == 'copy_operations':
        db.execute('DELETE FROM copy_operation_files WHERE namespace=%s AND operation=%s', key)
        manifest = record.get('manifest', {})
        if not isinstance(manifest,dict) or any(type(k) is not str or type(v) is not str for k,v in manifest.items()):
            raise LibraryError('Incompatible operational value: copy_operations.manifest')
        for name, digest in manifest.items():
            db.execute('INSERT INTO copy_operation_files VALUES (%s,%s,%s,%s)', (*key,name,digest))


def compose(table: str, record: dict[str,Any], scalars: tuple[Any,...]) -> dict[str,Any]:
    """Read canonical scalars while retaining exact codec presence/date shapes.

    A disagreement is corruption, not an independent JSON edit to accept. Date
    strings and structured policy evidence retain their original representation.
    """
    for (column,path,kind), expected, stored in zip(FIELDS[table],values(table,record),scalars,strict=True):
        comparable=datetime.fromisoformat(expected) if expected is not None and kind==Z else expected
        if comparable != stored:
            raise LibraryError(f'Operational consistency mismatch: {table}.{column}')
        parts=path.split('.')
        parent=record
        for part in parts[:-1]:
            if not isinstance(parent.get(part),dict):
                break
            parent=parent[part]
        else:
            leaf=parts[-1]
            if leaf in parent and kind!=Z and path!='remote_deadline' and not (table=='source_registry' and column=='last_scan_order'):
                original=parent[leaf]
                if original is None or type(original) in (str,bool,int,float):
                    # SQL float does not erase an original integer's codec type.
                    parent[leaf]=int(stored) if type(original) is int and stored is not None else stored
    return record


def records(db: DeadlineConnection, table: str, suffix: str = '', params: tuple[Any,...] = ()) -> list[tuple[Any,...]]:
    """Closed operational owners' reads. Callers provide only fixed SQL clauses."""
    from library.codec import decode
    keys=KEYS[table]
    raw='binding' if table=='source_inputs' else 'payload'
    columns=','.join((*keys,raw,*(name for name,_,_ in FIELDS[table])))
    result=[]
    for row in db.execute(f'SELECT {columns} FROM {table} '+suffix,params):
        value=decode(row[len(keys)]) if row[len(keys)] is not None else {}
        compose(table,value,row[len(keys)+1:])
        result.append((*row[:len(keys)],value))
    if table=='collection_submissions' and result:
        children: dict[tuple[Any,...],list[Any]] = {}
        for source,role,position,url in db.execute('SELECT source,role,position,url FROM submission_links WHERE source=ANY(%s) ORDER BY source,role,position',([row[0] for row in result],)):
            children.setdefault((source,role),[]).append(url)
        for source,record in result:
            for role in ('links','all_links','primary_links'):
                if role in record:
                    child=children.get((source,role),[])
                    if child!=record[role]:
                        raise LibraryError(f'Operational consistency mismatch: submission_links.{role}')
                    record[role]=child
    elif table=='routing_packages' and result:
        members: dict[tuple[Any,...],list[tuple[Any,Any]]] = {}
        for pid,role,position,member_key,payload in db.execute('SELECT pid,role,position,member_key,payload FROM routing_members WHERE pid=ANY(%s) ORDER BY pid,role,position',([row[0] for row in result],)):
            members.setdefault((pid,role),[]).append((member_key,decode(payload)))
        for pid,record in result:
            for role in ('primary_links','fallback_links','original_files','attempt_files','input_folders','files'):
                if role in record:
                    items=members.get((pid,role),[])
                    routing_child=dict(items) if isinstance(record[role],dict) else [value for _,value in items]
                    if encode(routing_child)!=encode(record[role]):
                        raise LibraryError(f'Operational consistency mismatch: routing_members.{role}')
                    record[role]=routing_child
    elif table=='copy_operations' and result:
        manifests: dict[tuple[Any,...],dict[str,str]] = {}
        for namespace,operation,name,digest in db.execute('SELECT namespace,operation,name,digest FROM copy_operation_files WHERE namespace=ANY(%s) AND operation=ANY(%s) ORDER BY namespace,operation,name',
                ([row[0] for row in result],[row[1] for row in result])):
            manifests.setdefault((namespace,operation),{})[name]=digest
        for namespace,identity,record in result:
            manifest_child=manifests.get((namespace,identity),{})
            if manifest_child!=record.get('manifest',{}):
                raise LibraryError('Operational consistency mismatch: copy_operation_files')
            if 'manifest' in record:
                record['manifest']=manifest_child
    return result


ASSET_CHILD_FIELDS = (
    ('asset_metadata','evidence','stage_metadata.evidence',T),
    ('asset_metadata','evaluation','stage_metadata.evaluation',T),
    ('asset_metadata','outcome','stage_metadata.outcome',T),
    ('asset_metadata','method','stage_metadata.method',T),
    ('asset_metadata','evaluated_at','stage_metadata.at',Z),
    ('asset_metadata','next_at','stage_metadata.next_at',Z),
    ('asset_admission','operation','admission.operation',T),
    ('asset_admission','status','admission.status',T),
    ('asset_admission','signature','admission.signature',T),
    ('asset_admission','target','admission.target',T),
)


def entity_columns(kind: str) -> str:
    result=['payload',*(name for name,_,_ in FIELDS[kind])]
    if kind=='assets':
        result.extend(f'(SELECT {column} FROM {table} WHERE asset=assets.id)' for table,column,_,_ in ASSET_CHILD_FIELDS)
    return ','.join(result)


def entity_record(kind: str, record: dict[str,Any], scalars: tuple[Any,...]) -> dict[str,Any]:
    size=len(FIELDS[kind])
    compose(kind,record,scalars[:size])
    if kind=='assets':
        for (_,column,path,datatype),stored in zip(ASSET_CHILD_FIELDS,scalars[size:],strict=True):
            value=at(record,path)
            if value is not None and datatype==Z:
                value=datetime.fromisoformat(value)
            if value!=stored:
                raise LibraryError(f'Operational consistency mismatch: assets.{path}')
    return record
