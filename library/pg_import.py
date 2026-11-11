"""Frozen SQLite/YAML inputs for the one-way PostgreSQL migration.

This is an explicit offline tool. No runtime caller reads these source files.
Input layout: catalogue.sqlite[/-wal], files/<library-relative paths>,
collector/<collector-relative paths>, optional activity.sqlite[/-wal].
Media stay in place and are bound explicitly by namespace and digest.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from contextlib import closing
from typing import Any, Callable

import yaml

from library.authority import code_digest
from library.codec import encode
from library.primitives import LibraryError
from library.postgres import Database


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _write_json(path: Path, value: Any, *, mode: int=0o400) -> None:
    """Exclusive immutable report creation, or exact interrupted-write readback."""
    from library.primitives import sync_dir
    raw=(json.dumps(value,sort_keys=True,ensure_ascii=False,indent=2)+'\n').encode()
    if path.exists():
        if path.is_symlink() or path.read_bytes()!=raw:raise LibraryError('Existing import artifact differs')
        return
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    descriptor,name=tempfile.mkstemp(dir=path.parent,prefix='.pg-import-')
    with os.fdopen(descriptor,'wb') as stream:
        os.fchmod(stream.fileno(),mode);stream.write(raw);stream.flush();os.fsync(stream.fileno())
    # The importer is the sole migration operator. Database ownership fences
    # every phase; an existing different artifact is never replaced.
    if path.exists():raise LibraryError('Import artifact appeared concurrently')
    os.rename(name,path);sync_dir(path.parent)


def target_identity(database: Database) -> dict[str,Any]:
    with database.connection() as db:
        row=db.execute('SELECT system_identifier::text,current_database(),'
            '(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),'
            'current_setting(\'server_version_num\')::int FROM pg_control_system()').fetchone()
    if row is None or row[3]<180006:raise LibraryError('Qualified PostgreSQL target required')
    return {'system_identifier':row[0],'database':row[1],'database_oid':row[2],
            'schema':database.schema,'server_version':row[3]}


def _catalogue_rows(source: Path) -> tuple[dict[str,list[dict[str,Any]]],dict[str,Any]]:
    from library.primitives import TABLES
    from library.store import RELATIONS, QUERY_COLUMNS, query_values
    from library.codec import decode
    rows,_=_sqlite(source,'catalogue.sqlite')
    meta=rows.pop('meta')[0]
    allowed=set(TABLES)|set(RELATIONS)|{'asset_flags','ledger_records','decision_editions',
        'decision_pieces','preview_bindings','edition_claims','decision_claims','case_bindings',
        'attachments','operator_rows','stage_schema','stage_tasks'}
    if set(rows)-allowed:raise LibraryError('Unclassified SQLite table')
    rows.pop('stage_schema',None)
    for kind in TABLES:
        for row in rows.get(kind,[]):
            record=decode(row['payload'])['record']
            for column,value in zip(QUERY_COLUMNS,query_values(kind,record)):
                row.setdefault(column,value)
    # The projection is retained exactly. Only its new binary sort index is derived.
    from library.sorting import storage_key
    rows['operator_sort']=[{'id':row['id'],'field':field,'value':storage_key(str(row[column]))}
        for row in rows.get('operator_rows',[]) for field,column in
        [(name,{'source_id':'source_ref'}.get(name,name)) for name in
         ('title','author','isbn','series','status','transfer','intake','identity','publication','reason','ocr')]]
    return rows,meta


def _table_digest(rows: list[dict[str,Any]]) -> str:
    return _fingerprint(sorted((encode(row) for row in rows)))


def _read_table(database: Database, table: str, columns: list[str]) -> list[dict[str,Any]]:
    from psycopg import sql
    with database.connection() as db:
        records=db.execute(sql.SQL('SELECT {} FROM {}').format(
            sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(table))).fetchall()
    return [dict(zip(columns,(bytes(value) if isinstance(value,memoryview) else value for value in row))) for row in records]


def _destination(path: Path, frozen: Path) -> None:
    if (not path.is_absolute() or path!=path.resolve(strict=False) or path.is_relative_to(frozen)
            or any(parent.is_symlink() for parent in (path,*path.parents))):
        raise LibraryError('Unsafe or frozen target path')


def _binding(manifest: dict[str,Any], database: Database, directory: Path) -> dict[str,Any]:
    _destination(directory,Path(manifest['source']['root']))
    return {'state_directory':str(directory),'manifest_sha256':manifest['sha256'],'code_sha256':manifest['code_sha256'],
            'candidate':manifest['candidate'],'target':target_identity(database),
            'authority_id':manifest['source']['authority_id'],'source_meta':manifest['source']['original_meta']}


def _previous_selector(manifest: dict[str,Any], directory: Path) -> None:
    path=directory/'storage-authority.yaml'
    expected=manifest['source']['files'].get('files/storage-authority.yaml')
    if path.exists():
        if path.is_symlink() or expected is None or _hash(path)!=expected['sha256']:
            raise LibraryError('Current selector is not the frozen legacy selector')
        value=yaml.safe_load(path.read_text())
        if value.get('backend')!='sqlite' or value.get('authority_id')!=manifest['source']['authority_id']:
            raise LibraryError('Current authority is not the bound legacy source')


def import_snapshot(manifest: dict[str,Any] | Path, directory: Path, *, database: Database,
                    checkpoint: Callable[[str],None] | None=None) -> None:
    """Resume only the same unselected import. Never overwrite selected business state."""
    from library.codec import decode
    from library.primitives import empty
    from library.store import Store
    from library.stage_tasks import StageTasks
    from library.launcher_access import install
    from psycopg import sql
    value=read_manifest(manifest)
    binding=_binding(value,database,directory)
    source=Path(value['source']['root'])
    rows,meta=_catalogue_rows(source)
    plans=_plans(source,rows,meta,value['source'].get('origins',{}))
    identity_file=directory/'postgresql-import.json'
    if identity_file.exists() and json.loads(identity_file.read_text())!=binding:
        raise LibraryError('Import target binding differs')
    with database.connection() as db:
        exists=db.execute('SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)',(database.schema,)).fetchall()[0][0]
    if not exists:
        directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        _previous_selector(value,directory)
        # Durable local binding precedes schema initialization. No credential is recorded.
        if not identity_file.exists():
            _write_json(identity_file,binding)
        state=empty();state['revision']=meta['global_revision'];state['ledger']=decode(meta['ledger'])
        Store.create(directory,database=database,authority=meta['authority_id'],state=state,
                     source_digest=value['sha256'],semantic=value['source']['catalogue_semantic_sha256'],import_identity=binding)
    with database.connection() as db:
        if db.execute("SELECT to_regclass('migration_import')").fetchall()[0][0] is None:
            raise LibraryError('Target does not belong to this import')
        saved=db.execute('SELECT identity,phase FROM migration_import WHERE singleton=1').fetchone()
        if saved is None or decode(saved[0])!=binding:raise LibraryError('Import identity differs')
        if db.execute('SELECT ready FROM meta').fetchall()[0][0]!=0:
            raise LibraryError('Selected target cannot be imported again')
        phase=saved[1]
    store=Store(directory,authority=meta['authority_id'],database=database)
    if phase=='initializing':
        StageTasks.upgrade(store,importing=True)
        with database.connection() as db:
            installed=db.execute("SELECT to_regprocedure('reader_accept(text)')").fetchall()[0][0] is not None
        if not installed:install(database)
        with database.connection(write=True) as db:
            db.execute("UPDATE migration_import SET phase='copying'")
    # Validate every already committed phase before the first resumed write.
    with database.connection() as db:
        completed=dict(db.execute('SELECT name,digest FROM migration_phases'))
    for table,records in plans.items():
        columns=list(records[0]) if records else []
        if table in completed:
            observed=_read_table(database,table,columns) if records else _empty_table(database,table)
            if completed[table]!=_table_digest(records) or _table_digest(observed)!=completed[table]:
                raise LibraryError('Completed import phase changed')
        elif _empty_table(database,table):
            raise LibraryError('Uncommitted target rows require inspection')
    for table,records in plans.items():
        if table in completed:continue
        stamp=_table_digest(records)
        with database.connection(write=True) as db:
            current=db.execute('SELECT identity,phase FROM migration_import FOR UPDATE').fetchone()
            if current is None or decode(current[0])!=binding or db.execute('SELECT ready FROM meta').fetchall()[0][0]!=0:
                raise LibraryError('Import ownership changed')
            if db.execute('SELECT count(*) FROM '+table).fetchall()[0][0]:
                raise LibraryError('Uncommitted target rows require inspection')
            if records:
                columns=list(records[0])
                statement=sql.SQL('COPY {} ({}) FROM STDIN').format(sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,columns)))
                with db.cursor().copy(statement) as writer:
                    for record in records:writer.write_row([record[column] for column in columns])
            if table in ('decoder_children','network_starts'):
                # COPY supplies historical positions; advance the identity used by
                # subsequent public writes before this phase becomes durable.
                db.execute("SELECT setval(pg_get_serial_sequence(%s,'position'),"
                    '(SELECT coalesce(max(position),0)+1 FROM '+table+'),false)',(table,))
            db.execute('INSERT INTO migration_phases VALUES(%s,%s)',(table,stamp))
        if checkpoint:checkpoint('catalogue' if table=='operator_sort' else table)
    # Check bytes again after copy. A changed source can never become selectable.
    read_manifest(value)
    with database.connection(write=True) as db:db.execute("UPDATE migration_import SET phase='imported'")


def _plans(source: Path, rows: dict[str,list[dict[str,Any]]], meta: dict[str,Any],
           origins: dict[str,str] | None=None) -> dict[str,list[dict[str,Any]]]:
    from library.primitives import TABLES
    from library.store import RELATIONS
    order=[*TABLES,*RELATIONS,'asset_flags','ledger_records','decision_editions','decision_pieces',
           'preview_bindings','edition_claims','decision_claims','case_bindings','attachments','operator_rows','operator_sort','stage_tasks']
    from library.pg_import_records import records
    result={table:rows.get(table,[]) for table in order}
    result.update(records(source,rows,origins=origins or {}))
    result.update(_decisions(source,rows,meta))
    if (source/'activity.sqlite').exists():
        activity,_=_sqlite(source,'activity.sqlite')
        if set(activity)!={'meta','producers','events'}:raise LibraryError('Unclassified Activity SQLite table')
        if len(activity['meta'])!=1 or activity['meta'][0].get('schema')!=1 or not activity['meta'][0].get('authority_id'):
            raise LibraryError('Incomplete Activity authority')
        legacy=source/'files'/'activity'/'activity-authority.yaml'
        if legacy.exists():
            from library.pg_import_records import load
            selected=load(legacy)
            if selected!={'schema':1,'authority_id':activity['meta'][0]['authority_id']}:
                raise LibraryError('Frozen Activity selector differs')
        for old,new in (('meta','activity_meta'),('producers','activity_producers'),('events','activity_events')):
            result[new]=activity[old]
    else:
        for table in ('activity_meta','activity_producers','activity_events'):result[table]=[]
    return result


def _decisions(source: Path, rows: dict[str,list[dict[str,Any]]], meta: dict[str,Any]) -> dict[str,list[dict[str,Any]]]:
    from library.codec import decode
    from library.primitives import digest
    from library.store import Store
    from library.pg_import_records import load
    decisions: list[dict[str,Any]]=[]
    for path in sorted((source/'files'/'library-decisions').glob('*.yaml')):
        record=load(path)
        if (record.get('id')!=digest({k:v for k,v in record.items() if k!='id'})
                or record.get('sequence')!=len(decisions)+1
                or record.get('previous')!=(decisions[-1]['id'] if decisions else None)
                or path.name!=f"{len(decisions)+1:09d}-{record['id']}.yaml"):
            raise LibraryError('Decision chain is not immutable and contiguous')
        decisions.append(record)
    indexed=sorted(rows.get('ledger_records',[]),key=lambda row:row['sequence'])
    applied=decode(meta['ledger'])
    if (applied!=[record['id'] for record in decisions[:len(applied)]]
            or len(indexed)<len(applied) or len(indexed)>len(decisions)
            or any(row['sequence']!=record['sequence'] or decode(row['payload'])!=record
                   or any(row.get(column)!=record.get(column) for column in ('id','target','action','request_id'))
                   for row,record in zip(indexed,decisions))):
        raise LibraryError('Catalogue decisions are not a retained immutable prefix')
    head=source/'files'/'library-ledger-head.yaml'
    if head.exists():
        value=load(head);count=value.get('count')
        if type(count) is not int or count<0 or count>len(decisions) or value.get('head')!=(decisions[count-1]['id'] if count else None):
            raise LibraryError('Retained decision head differs from chain')
    reservations=[];receipts=[]
    for index,record in enumerate(decisions):
        reservations.append({'sequence':record['sequence'],'id':record['id'],'target':record.get('target'),
            'payload':encode(record),'status':'projected' if index<len(applied) else 'indexed' if index<len(indexed) else 'reserved',
            'scope':encode(sorted(Store._decision_keys(record)-{'decision-chain'}))})
        if index<len(indexed):
            receipts.append({'key':'decision-index:'+record['id'],'digest':_fingerprint(record),
                'state':'committed','catalogue':False,'receipt':encode({'decision':record['id']})})
    return {'decision_reservations':reservations,'commit_receipts':receipts}


def _empty_table(database: Database, table: str) -> list[dict[str,Any]]:
    with database.connection() as db:
        count=db.execute('SELECT count(*) FROM '+table).fetchall()[0][0]
    return [] if count==0 else [{'unexpected_count':count}]


def compare(manifest: dict[str,Any] | Path, directory: Path, *, database: Database) -> dict[str,Any]:
    """Read-only source/target comparison; no mutator or runtime projection is called."""
    from library.codec import decode
    value=read_manifest(manifest)
    binding=_binding(value,database,directory)
    rows,meta=_catalogue_rows(Path(value['source']['root']))
    with database.connection() as db:
        saved=db.execute('SELECT identity,phase FROM migration_import').fetchone()
        if saved is None or decode(saved[0])!=binding or saved[1] not in ('imported','verified','selection_pending','selected'):
            raise LibraryError('Import is incomplete or target differs')
    reports={}
    for table,records in _plans(Path(value['source']['root']),rows,meta,value['source'].get('origins',{})).items():
        if records:
            observed=_read_table(database,table,list(records[0]))
        else:
            observed=_empty_table(database,table)
        reports[table]={'source_count':len(records),'target_count':len(observed),
                        'source_sha256':_table_digest(records),'target_sha256':_table_digest(observed)}
    expected={'authority_id':meta['authority_id'],'schema_version':1,'global_revision':meta['global_revision'],
        'ledger':meta['ledger'],'case_scope_revision':meta['case_scope_revision'],
        'imported_source_digest':value['sha256'],'source_semantic':value['source']['catalogue_semantic_sha256'],
        'required_runtime':'postgresql>=18.6'}
    meta_observed=_read_table(database,'meta',list(expected))[0]
    reports['meta']={'source_count':1,'target_count':1,'source_sha256':_fingerprint(expected),'target_sha256':_fingerprint(meta_observed)}
    equal=all(item['source_sha256']==item['target_sha256'] for item in reports.values())
    return {'schema':1,'status':'equal' if equal else 'different','binding':binding,'tables':reports}


def select(manifest: dict[str,Any] | Path, directory: Path, *, database: Database,
           checkpoint: Callable[[str],None] | None=None, collector_directory: Path | None=None) -> None:
    """Select only a semantically equal target. Old inputs stay immutable evidence."""
    from library.codec import decode
    from library.primitives import atomic_yaml
    value=read_manifest(manifest)
    report=compare(value,directory,database=database)
    if report['status']!='equal':raise LibraryError('Semantic comparison failed')
    config=directory/'postgresql.json'
    if not config.exists():raise LibraryError('Private target configuration required before selection')
    if target_identity(Database.configured(directory))!=report['binding']['target']:
        raise LibraryError('Private target selector differs')
    authority={'schema':2,'backend':'postgresql','authority_id':value['source']['authority_id'],
        'catalogue_path':'postgresql.json','source_digest':value['sha256'],
        'source_semantic_sha256':value['source']['catalogue_semantic_sha256'],
        'code_sha256':value['code_sha256'],'runtime':{'python_minor':[3,14],'postgresql_min':[18,6]},
        'compatible_legacy_commit':None}
    runtime_config=json.loads(config.read_text())
    _destination(Path(runtime_config['dsn_file']),Path(value['source']['root']))
    if json.loads((directory/'postgresql-import.json').read_text())!=report['binding']:
        raise LibraryError('Selected state directory is not the import target')
    selectors=[]
    if collector_directory is not None:
        _destination(collector_directory,Path(value['source']['root']))
        if str(collector_directory)!=value['source']['origins'].get('collector'):
            raise LibraryError('Collector destination is not the bound origin')
        selectors.append((collector_directory/'postgresql.json',runtime_config,0o600))
    activity_rows,_=_sqlite(Path(value['source']['root']),'activity.sqlite') if (Path(value['source']['root'])/'activity.sqlite').exists() else ({},'')
    if activity_rows:
        metas=activity_rows['meta']
        if len(metas)!=1 or metas[0]['schema']!=1:raise LibraryError('Invalid Activity authority')
        activity_directory=directory/'activity'
        _destination(activity_directory,Path(value['source']['root']))
        selectors.extend([(activity_directory/'postgresql.json',runtime_config,0o600),
            (activity_directory/'activity-authority.yaml',{'schema':2,'backend':'postgresql',
                'authority_id':metas[0]['authority_id']},0o400)])
    # Validate every existing auxiliary selector before any readiness write.
    for auxiliary,expected,mode in selectors:
        if auxiliary.exists():
            if auxiliary.is_symlink():raise LibraryError('Unsafe auxiliary selector')
            observed=json.loads(auxiliary.read_text()) if auxiliary.suffix=='.json' else yaml.safe_load(auxiliary.read_text())
            if observed==expected:continue
            key='files/activity/activity-authority.yaml'
            old=value['source']['files'].get(key)
            if auxiliary.name!='activity-authority.yaml' or old is None or _hash(auxiliary)!=old['sha256']:
                raise LibraryError('Auxiliary selector differs')
            if observed.get('authority_id')!=metas[0]['authority_id'] or set(observed)!={'schema','authority_id'} or observed.get('schema')!=1:
                raise LibraryError('Legacy Activity selector differs')
    path=directory/'storage-authority.yaml'
    already_selected=False
    if path.exists():
        saved_selector=yaml.safe_load(path.read_text())
        already_selected=saved_selector==authority
        if not already_selected:_previous_selector(value,directory)
    with database.connection(write=True) as db:
        saved=db.execute('SELECT identity,phase FROM migration_import FOR UPDATE').fetchone()
        if saved is None or decode(saved[0])!=report['binding']:
            raise LibraryError('Import binding changed')
        db.execute('UPDATE meta SET ready=1')
        db.execute("UPDATE migration_import SET phase='selection_pending'")
    if checkpoint:checkpoint('selection_pending')
    # Resume any interrupted auxiliary selector installation before final activation.
    for auxiliary,expected,mode in selectors:
        if auxiliary.suffix=='.json':_write_json(auxiliary,expected,mode=mode)
        else:
            auxiliary.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            atomic_yaml(auxiliary,expected);auxiliary.chmod(mode)
    # The deployment writes the private credential selector explicitly, never a DSN in proof.
    if already_selected:
        if yaml.safe_load(path.read_text())!=authority:raise LibraryError('Selected authority differs')
    else:
        atomic_yaml(path,authority);path.chmod(0o400)
    with database.connection(write=True) as db:db.execute("UPDATE migration_import SET phase='selected'")


LIBRARY_INPUTS = {
    'task-policy.yaml':'all_task_policy', 'stage-resources.yaml':'resources',
    'stage-children':'child_ownership', 'reader-dependency-children.yaml':'child_ownership',
    'dependency-readiness.yaml':'readiness', 'stage-discovery.yaml':'stage_cursors',
    'stage-intake-discovery.yaml':'stage_cursors', 'library-mirror-cursor.yaml':'stage_cursors',
    'library-operations':'publication', 'library-admission-operations':'publication',
    'library-provisional-operations':'publication', 'library-resolution':'resolution_previews',
    'library-decisions':'decision_chain', 'library-ledger-head.yaml':'decision_chain',
    'proof-requests':'manual_proofs', 'reader-requests':'reader_jobs', 'reader-jobs':'reader_jobs',
    'provider-admission.yaml':'network_admission', 'native-admission.yaml':'network_admission',
    'library-network-budget.yaml':'network_admission', 'native-effects.yaml':'native_pointer',
    'stage-activation.yaml':'activation', 'library-activation.yaml':'activation',
    'library-retirement.yaml':'library_retirement', 'snapshots':'observations',
    'evidence':'observations', 'compose-diagnostic-sources.yaml':'manual_and_diagnostic_bindings',
    'provider-cache':'provider_cache', 'library-provider-cache':'provider_cache', 'french-source-cache':'immutable_files',
    'stage-actors':'observations',
    'automatic-recovery.yaml':'legacy_recovery', 'restore-receipt.yaml':'legacy_recovery',
    'storage-transitions':'legacy_recovery', 'storage-authority.yaml':'catalogue_authority',
    'activity-authority.yaml':'activity', 'library.yaml':'catalogue_authority',
    'backfill-history':'immutable_files', 'relocation.json':'configuration',
}
COLLECTOR_INPUTS = {'state.yaml':'captured_sources','monitor.yaml':'collection_checkpoints',
    'mirror-routing.yaml':'routing','package-retirement.yaml':'package_retirement',
    'backfill.yaml':'backfill','fiches.yaml':'fiches','backfill-history':'immutable_files',
    'catalogue.yaml':'observations','all-links.txt':'observations','activity':'activity'}

EVIDENCE_ARCHIVES = {
    'acceptance.yaml','activate.json','activity-99492ee','asset-attempts-resume.log','asset-attempts.log',
    'asset-attempts.yaml','automatic-cycle.stderr','automatic-cycle.stdout','design-7iazm','enrich.json',
    'format-retry','https-public-origin','identity-coverage.yaml','independent-stage-queues-20261109',
    'independent-stage-queues-main-20261109','independent-stage-queues-qualification-20261109',
    'independent-stage-queues-qualification-20261109.yaml','independent-stage-queues-qualification-contention-20261109.yaml',
    'independent-stage-queues-qualification-control-20261109.yaml','independent-stage-queues-qualification-final-runtime-20261109.yaml',
    'independent-stage-queues-qualification-native-memory-20261109.yaml','independent-stage-queues-qualification-resized-20261109.yaml',
    'intake.json','metadata-9tiyu','path-correction.yaml','printed.json','publish.json','read-api-reliability',
    'reader-recovery-20261109','reconcile.json','recovery-reconcile.json','recovery-reconcile.stderr',
    'recovery-restore.json','recovery-restore.stderr','recovery-withdraw.json','recovery-withdraw.stderr',
    'repeat-acceptance.yaml','repeat-before.yaml','repeat-cycle.json','retirement-activation','retirement-stock',
    'selected-replay-2v9ed','selected-replay-2v9ed-2','selected-replay-2v9ed-3','selected-replay-2v9ed-4',
    'selected-replay-2v9ed-5','source-before.yaml','storage-0005-20261109-activity-fix-drain',
    'storage-0005-20261109-convert','storage-0005-20261109-drain','storage-0005-20261109-fix-activation',
    'storage-0005-20261109-fix-drain','storage-0005-20261109-fix-quiesce','storage-0005-20261109-fix-runtime',
    'storage-0005-20261109-preflight.json','storage-0005-20261109-quiesce','storage-0005-20261109-resume',
    'storage-0005-20261109-runtime','storage-0005-20261109-select','summary.md','web-covers','reader-probes',
}


def _hash(path: Path) -> str:
    with path.open('rb') as stream:
        before=path.stat()
        result=hashlib.file_digest(stream,'sha256').hexdigest()
        after=path.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
        raise LibraryError('Frozen source changed during read')
    return result


def _role(relative: Path, hoster_exports: set[str] | None=None) -> tuple[str,str]:
    parts=relative.parts
    if str(relative) in ('state/catalogue/catalogue.sqlite','state/catalogue/catalogue.sqlite-wal','state/catalogue/catalogue.sqlite-shm'):
        return 'catalogue_entities','retained' if str(relative).endswith('-shm') else 'source-database'
    if parts[0] in ('catalogue.sqlite','catalogue.sqlite-wal','catalogue.sqlite-shm'):
        return 'catalogue_entities','source-database' if not parts[0].endswith('-shm') else 'retained'
    if parts[0] in ('activity.sqlite','activity.sqlite-wal','activity.sqlite-shm'):
        return 'activity','source-database' if not parts[0].endswith('-shm') else 'retained'
    if parts[0]=='recovery-manifest.yaml':return 'legacy_recovery','retained'
    if parts[0] in ('payloads','attachments','pieces'):return 'immutable_files','retained'
    if parts[0] not in ('files','collector') or len(parts)<2:
        raise LibraryError('Unclassified frozen input')
    name=parts[1]
    if parts[0]=='files' and name=='activity':
        if len(parts)==2:return 'activity','retained'
        if len(parts)==3 and parts[2]=='activity-authority.yaml':return 'activity','retained'
        if len(parts)==3 and parts[2] in ('activity.sqlite-shm','activity.sqlite.writer.lock'):
            return 'process_locks','retained'
        raise LibraryError('Activity database must use the unambiguous root input layout')
    mapping=LIBRARY_INPUTS if parts[0]=='files' else COLLECTOR_INPUTS
    if len(parts)>2 and parts[2].startswith('.'):return 'retained_temporary_files','retained'
    if parts[0]=='collector' and name.endswith('.txt'):
        if name=='all-links.txt' or name in (hoster_exports or set()):return 'observations','derived'
    if name=='evidence' and len(parts)>2:
        item=parts[2]
        if item in EVIDENCE_ARCHIVES:return 'immutable_files','retained'
        if item.endswith('.lock'):return 'process_locks','retained'
        if item in ('dashboard.yaml','review.yaml','review.md'):return 'observations','derived'
        if item=='activity':return 'activity','retained'
        if item not in ('activity-dashboard.yaml','browser-session.yaml','cycle.yaml','retirement-cycle.yaml','mirror-routing-cycle.yaml','stage-actors'):
            raise LibraryError('Unclassified evidence input')
    if name=='snapshots' and len(parts)>2:
        if parts[2] in ('captured.yaml','submissions.yaml'):return 'observations','derived'
        if parts[2] not in ('queue.yaml','external-capacity.yaml'):raise LibraryError('Unclassified snapshot input')
    if name=='library-resolution' and len(parts)>2:
        if parts[2]=='pieces':return 'immutable_files','retained'
        if parts[2]!='previews':raise LibraryError('Unclassified resolution input')
        if len(parts)>3 and (len(parts)!=4 or not parts[3].endswith('.yaml')):raise LibraryError('Unclassified preview input')
    if name in ('library-operations','library-admission-operations','library-provisional-operations') and len(parts)>2:
        if parts[2]=='evidence':return 'immutable_files','retained'
        if len(parts)!=3 or not parts[2].endswith('.yaml'):raise LibraryError('Unclassified copy operation input')
    if name=='reader-requests' and len(parts)>3:
        if len(parts)!=4 or parts[3] not in ('manifest.json','grant.json'):raise LibraryError('Unclassified reader request input')
    if name=='reader-jobs' and len(parts)>3:
        if parts[3] in ('work','input','parts','worker.stdout.log','worker.stderr.log','manifest.json'):
            return 'immutable_files','retained'
        if parts[3].endswith('.lock'):return 'process_locks','retained'
        if len(parts)!=4 or parts[3] not in ('intent.yaml','container.yaml','receipt.yaml','reconciled.yaml','cleanup.yaml'):
            raise LibraryError('Unclassified reader job input')
    if name in ('library-decisions','stage-children','stage-actors','proof-requests','provider-cache','library-provider-cache') and len(parts)>2:
        if len(parts)!=3 or not parts[2].endswith('.yaml'):raise LibraryError('Unconsumed mutable input')
    if name.startswith('stage-selection-') and name.endswith('.yaml'):
        return 'stage_cursors','imported'
    if name.startswith('.'):
        return 'retained_temporary_files','retained'
    if name.endswith('.lock') or name=='stage-scopes':
        return 'process_locks','retained'
    if name.startswith('library-pre-sqlite-'):
        return 'legacy_recovery','retained'
    if name not in mapping:raise LibraryError('Unclassified frozen input')
    family=mapping[name]
    if parts[0]=='collector' and name=='activity':return family,'retained'
    if name in ('library-mirror-cursor.yaml','library-network-budget.yaml'):return family,'retained'
    role='retained' if family in ('immutable_files','configuration','legacy_recovery','process_locks') else 'imported'
    return family,role


def inventory(source: Path) -> dict[str,Any]:
    """Reject unknown inputs and symlinks; hash every retained regular file."""
    if source.is_symlink() or not source.is_dir():raise LibraryError('Frozen source root required')
    hoster_exports=set()
    state=source/'collector'/'state.yaml'
    if state.exists() and not state.is_symlink():
        import re
        from library.pg_import_records import load
        for item in load(state).get('items',{}).values():
            for entry in item.get('downloads',{}).values():
                if isinstance(entry.get('hoster'),str):
                    hoster_exports.add((re.sub(r'[^a-z0-9-]+','-',entry['hoster'].lower()).strip('-') or 'unnamed')+'.txt')
    result={}
    for path in sorted(source.rglob('*')):
        if path.is_symlink():raise LibraryError('Symlink in frozen input')
        relative=path.relative_to(source)
        if str(relative) in ('files','collector','state','state/catalogue'):continue
        family,role=_role(relative,hoster_exports)
        if path.is_dir():continue
        if not path.is_file():raise LibraryError('Nonregular frozen input')
        result[str(relative)]={'family':family,'role':role,'bytes':path.stat().st_size,'sha256':_hash(path)}
    if not ({'catalogue.sqlite','state/catalogue/catalogue.sqlite'}&result.keys()):raise LibraryError('Frozen catalogue is absent')
    if {'catalogue.sqlite','state/catalogue/catalogue.sqlite'}<=result.keys():raise LibraryError('Ambiguous frozen catalogue sources')
    return result


def _sqlite(source: Path, name: str) -> tuple[dict[str,list[dict[str,Any]]],str]:
    """Copy the main file AND WAL before opening SQLite. Never touch source SHM."""
    private=Path(tempfile.mkdtemp(prefix='booksys-pg-import-sqlite-'))
    private.chmod(0o700)
    for suffix in ('','-wal'):
        path=source/(name+suffix)
        if not (source/name).exists() and name=='catalogue.sqlite':path=source/'state'/'catalogue'/(name+suffix)
        if path.exists():shutil.copyfile(path,private/(name+suffix))
    with closing(sqlite3.connect((private/name).as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        if db.execute('PRAGMA integrity_check').fetchall()[0][0]!='ok':
            raise LibraryError('Frozen SQLite integrity failed')
        names=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        rows={name:[dict(row) for row in db.execute('SELECT * FROM "'+name.replace('"','""')+'"')] for name in names}
    return rows,str(private)


def prepare_manifest(source: Path, *, candidate: str, media: dict[str,Any] | None=None,
                     origins: dict[str,str] | None=None) -> dict[str,Any]:
    """Bind the frozen bytes and exact code before any target write."""
    source=source.resolve()
    for key,origin in (origins or {}).items():
        if key not in ('library','collector','reader_jobs'):raise LibraryError('Unclassified origin')
        path=Path(origin)
        if not path.is_absolute() or path!=path.resolve(strict=False):raise LibraryError('Canonical origin required')
    files=inventory(source)
    rows,private=_sqlite(source,'catalogue.sqlite')
    meta=rows.get('meta',[])
    if len(meta)!=1 or meta[0]['ready']!=1 or meta[0]['schema_version']!=1:
        raise LibraryError('Frozen catalogue authority is incomplete')
    _verify_media(source,rows,media or {})
    _verify_decision_proofs(source,media or {})
    semantic=_catalogue_semantic(rows,meta[0],source,media or {})
    _verify_legacy_manifest(source,files,rows,meta[0],semantic,media or {})
    plans=_plans(source,{k:v for k,v in rows.items() if k!='meta'},meta[0],origins)
    if inventory(source)!=files:raise LibraryError('Frozen source changed')
    result={'schema':1,'candidate':candidate,'code_sha256':code_digest(),'target_schema_version':1,
        'source':{'root':str(source),'authority_id':meta[0]['authority_id'],'revision':meta[0]['global_revision'],
                  'catalogue_semantic_sha256':semantic,'original_meta':meta[0],'origins':origins or {},
                  'files':files,'families':_family_report(files,plans)},'media':media or {}}
    result['sha256']=hashlib.sha256(encode(result).encode()).hexdigest()
    return result


# Concrete ownership from the accepted 38-family inventory. Shared source files
# can feed several normalized tables; counts describe records, not payload copies.
FAMILY_TABLES = {
    'catalogue_entities':'works editions variants assets artifacts origins cases operations',
    'catalogue_relations':'asset_flags edition_work variant_edition variant_asset asset_origin origin_parent selected_variant artifact_asset artifact_edition operation_edition edition_publication_asset identity_work identity_edition identity_variant edition_claims',
    'catalogue_projection':'operator_rows operator_sort case_bindings',
    'catalogue_authority':'', 'all_task_policy':'stage_tasks', 'source_registry':'source_registry source_inputs',
    'stage_cursors':'stage_selection stage_discovery', 'resources':'resource_pools resource_grants resource_waiters',
    'child_ownership':'decoder_owners decoder_children','reader_jobs':'reader_requests reader_job_state',
    'manual_proofs':'manual_proofs','captured_sources':'captured_sources captured_downloads captured_download_links',
    'watermarks':'collection_categories','collection_checkpoints':'collection_checkpoints checkpoint_members checkpoint_targets checkpoint_downloads checkpoint_links',
    'collection_schedule':'collection_checkpoints','submissions':'collection_submissions','fiches':'fiche_progress',
    'backfill':'collection_backfill backfill_selection backfill_pages backfill_items backfill_page_items',
    'routing':'routing_packages','package_retirement':'retirement_acquisitions retired_packages retired_links retirement_cursor',
    'library_retirement':'','publication':'copy_operations','resolution_previews':'resolution_previews preview_bindings',
    'decision_chain':'ledger_records decision_reservations commit_receipts decision_editions decision_pieces decision_claims',
    'activity':'activity_meta activity_producers activity_events','readiness':'dependency_readiness',
    'network_admission':'network_budgets network_starts','native_pointer':'native_effect',
    'provider_cache':'provider_cache','activation':'runtime_activation','legacy_recovery':'',
    'observations':'native_observation observed_packages observed_links actor_heartbeats activity_observation browser_observation cycle_status retirement_observation routing_scheduler',
    'manual_and_diagnostic_bindings':'compose_sources diagnostic_requests','immutable_files':'attachments',
    'configuration':'','process_locks':'','pyload':'','retained_temporary_files':''}


def _family_report(files: dict[str,Any], plans: dict[str,list[dict[str,Any]]]) -> dict[str,Any]:
    result={}
    for family,tables in FAMILY_TABLES.items():
        matched={name:entry['role'] for name,entry in files.items() if entry['family']==family}
        counts={table:len(plans.get(table,[])) for table in tables.split()}
        present=bool(matched) or any(counts.values())
        result[family]={'presence':'present' if present else 'absent', 'files':matched,'target_counts':counts}
    result['catalogue_authority']['presence']='present'
    result['pyload']['presence']='preserved_external'
    result['pyload']['policy']='Preserve the sole downloader, its internal database, queue and accounts in place; no import or test effects.'
    return result


def read_manifest(manifest: dict[str,Any] | Path) -> dict[str,Any]:
    if isinstance(manifest,Path) and (manifest.is_symlink() or manifest.stat().st_mode&0o222):
        raise LibraryError('Import manifest must be immutable')
    value=json.loads(manifest.read_text()) if isinstance(manifest,Path) else manifest
    if (value.get('schema')!=1 or value.get('target_schema_version')!=1
            or value.get('code_sha256')!=code_digest()
            or value.get('sha256')!=hashlib.sha256(encode({k:v for k,v in value.items() if k!='sha256'}).encode()).hexdigest()):
        raise LibraryError('Import manifest or candidate differs')
    if inventory(Path(value['source']['root']))!=value['source']['files']:
        raise LibraryError('Frozen input differs from manifest')
    _verify_media(Path(value['source']['root']),None,value['media'])
    return value


def inspect_target(database: Database) -> dict[str,Any]:
    """Read current PostgreSQL progress. This never reads or reapplies an old source."""
    from library.codec import decode
    identity=target_identity(database)
    with database.connection() as db:
        meta=db.execute('SELECT authority_id,global_revision,ready FROM meta').fetchone()
        operation=db.execute('SELECT identity,phase FROM migration_import').fetchone()
        phases=list(db.execute('SELECT name,digest FROM migration_phases ORDER BY name'))
        committed=db.execute("SELECT count(*) FROM commit_receipts WHERE catalogue AND state='committed'").fetchall()[0][0]
    if meta is None or operation is None:raise LibraryError('Current import target is absent')
    return {'schema':1,'target':identity,'authority_id':meta[0],'revision':meta[1]+committed,
            'ready':bool(meta[2]),'phase':operation[1],'operation':decode(operation[0]),
            'phases':[{'name':name,'sha256':stamp} for name,stamp in phases]}


def main(argv: list[str] | None=None) -> int:
    """Versioned one-way migration command. Error output contains no source payload."""
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    inspect=commands.add_parser('inspect');inspect.add_argument('--report',type=Path,required=True)
    for name in ('inventory','manifest'):
        command=commands.add_parser(name);command.add_argument('--source',type=Path,required=True)
        command.add_argument('--output',type=Path,required=True)
        if name=='manifest':
            command.add_argument('--candidate',required=True)
            command.add_argument('--media-bindings',type=Path)
            command.add_argument('--origins',type=Path)
    for name in ('import','compare','select'):
        command=commands.add_parser(name);command.add_argument('--manifest',type=Path,required=True)
        command.add_argument('--state',type=Path,required=True)
        command.add_argument('--expected-manifest-sha256')
        if name=='compare':command.add_argument('--report',type=Path,required=True)
        if name=='select':
            command.add_argument('--runtime-dsn-file',type=Path)
            command.add_argument('--collector-state',type=Path)
    for name in ('inspect','import','compare','select'):
        command=commands.choices[name]
        command.add_argument('--dsn-file',type=Path,required=True)
        command.add_argument('--namespace',required=True)
    args=parser.parse_args(argv)
    value: dict[str,Any]
    try:
        if args.command=='inventory':
            if args.output.resolve().is_relative_to(args.source.resolve()):raise LibraryError('Report cannot change frozen input')
            value={'schema':1,'source':str(args.source.resolve()),'files':inventory(args.source)}
            _write_json(args.output,value)
            result={'status':'inventoried','files':len(value['files'])}
        elif args.command=='manifest':
            if args.output.resolve().is_relative_to(args.source.resolve()):raise LibraryError('Manifest cannot change frozen input')
            value=prepare_manifest(args.source,candidate=args.candidate,
                media=json.loads(args.media_bindings.read_text()) if args.media_bindings else None,
                origins=json.loads(args.origins.read_text()) if args.origins else None)
            _write_json(args.output,value)
            result={'status':'bound','manifest_sha256':value['sha256'],'revision':value['source']['revision']}
        else:
            if not Database._private(args.dsn_file):raise LibraryError('Private migration credential required')
            database=Database(args.dsn_file.read_text().strip(),args.namespace)
            if args.command=='inspect':
                value=inspect_target(database);_write_json(args.report,value)
                result={'status':'current','phase':value['phase'],'revision':value['revision']}
            else:
                value=read_manifest(args.manifest)
                if args.expected_manifest_sha256 and value['sha256']!=args.expected_manifest_sha256:
                    raise LibraryError('Approved manifest differs')
                if args.command=='import':
                    import_snapshot(value,args.state,database=database);result={'status':'imported'}
                elif args.command=='compare':
                    report=compare(value,args.state,database=database);_write_json(args.report,report)
                    if report['status']!='equal':raise LibraryError('Semantic comparison differs')
                    result={'status':'equal','report_sha256':_hash(args.report)}
                else:
                    _destination(args.state,Path(value['source']['root']))
                    if json.loads((args.state/'postgresql-import.json').read_text())!=_binding(value,database,args.state):
                        raise LibraryError('Selection state is not the import target')
                    if args.collector_state is not None:
                        _destination(args.collector_state,Path(value['source']['root']))
                        if str(args.collector_state)!=value['source']['origins'].get('collector'):
                            raise LibraryError('Collector destination is not the bound origin')
                    if args.runtime_dsn_file:
                        _destination(args.runtime_dsn_file,Path(value['source']['root']))
                        if not Database._private(args.runtime_dsn_file):raise LibraryError('Private runtime credential required')
                        runtime=Database(args.runtime_dsn_file.read_text().strip(),args.namespace)
                        if target_identity(runtime)!=target_identity(database):raise LibraryError('Runtime target differs')
                        _write_json(args.state/'postgresql.json',{'schema':args.namespace,'dsn_file':str(args.runtime_dsn_file.absolute())},mode=0o600)
                    select(value,args.state,database=database,collector_directory=args.collector_state);result={'status':'selected'}
        print(json.dumps({'schema':1,**result},sort_keys=True));return 0
    except (LibraryError,OSError,ValueError,TypeError,KeyError,sqlite3.Error,yaml.YAMLError):
        print(json.dumps({'schema':1,'status':'blocked','reason':'Migration verification failed; preserve source and target'}));return 2


def _media_path(source: Path, media: dict[str,Any], namespace: str, relative: str) -> Path:
    path=Path(relative)
    if path.is_absolute() or '..' in path.parts:raise LibraryError('Unsafe media reference')
    root=Path(media[namespace]['root']) if namespace in media else source/namespace
    result=root/path
    if result.is_symlink() or any(parent.is_symlink() for parent in result.parents):
        raise LibraryError('Symlink in retained media reference')
    return result


def _verify_media(source: Path, rows: dict[str,list[dict[str,Any]]] | None, media: dict[str,Any]) -> None:
    for namespace,binding in media.items():
        if namespace not in ('attachments','pieces','reader-jobs','bundle-inputs','covers','library'):
            raise LibraryError('Unclassified media namespace')
        if (set(binding)!={'root','files'} or not Path(binding['root']).is_absolute()
                or Path(binding['root'])!=Path(binding['root']).resolve(strict=False)):
            raise LibraryError('Invalid media binding')
        for relative,expected in binding['files'].items():
            path=_media_path(source,media,namespace,relative)
            if not path.is_file() or _hash(path)!=expected:raise LibraryError('Retained media changed')
    if rows is not None:
        for row in rows.get('attachments',[]):
            identity=row['digest']
            if 'attachments' in media and identity not in media['attachments']['files']:
                raise LibraryError('Referenced attachment is not bound')
            path=_media_path(source,media,'attachments',identity)
            if not path.exists():path=source/'payloads'/identity
            if not path.is_file() or _hash(path)!=identity:raise LibraryError('Retained attachment is missing or changed')
            if path.stat().st_size!=row['original_length']:raise LibraryError('Attachment length differs')
        for row in rows.get('decision_pieces',[]):
            if 'pieces' in media and row['piece']+'.yaml' not in media['pieces']['files']:
                raise LibraryError('Referenced decision proof is not bound')
            path=_media_path(source,media,'pieces',row['piece']+'.yaml')
            if not path.is_file():raise LibraryError('Retained decision proof is missing')
            from library.pg_import_records import load
            from library.primitives import digest
            if digest(load(path))!=row['piece']:raise LibraryError('Decision proof changed')


def _verify_decision_proofs(source: Path, media: dict[str,Any]) -> None:
    """Bind proofs referenced by every immutable record, including unindexed tails."""
    from library.pg_import_records import load
    from library.primitives import digest
    for path in (source/'files'/'library-decisions').glob('*.yaml'):
        record=load(path);values=record.get('values',{})
        pages=values.get('evidence',{}).get('pages',[]) if record.get('action')=='identify_work' else (
            values.get('proof',[]) if record.get('action')=='resolve' else [])
        references=[(proof['page'],True) for proof in pages]
        if record.get('action')=='resolve':references.extend((notice,False) for notice in values.get('notices',[]))
        for identity,asset_bound in references:
            relative=identity+'.yaml'
            if 'pieces' in media and relative not in media['pieces']['files']:
                raise LibraryError('Immutable decision proof is not bound')
            proof_path=_media_path(source,media,'pieces',relative)
            if not proof_path.exists():proof_path=source/'files'/'library-resolution'/'pieces'/relative
            piece=load(proof_path)
            if digest(piece)!=identity or asset_bound and piece.get('asset')!=record['target']:
                raise LibraryError('Immutable decision proof differs')


def _catalogue_semantic(rows: dict[str,list[dict[str,Any]]], meta: dict[str,Any], source: Path, media: dict[str,Any]) -> str:
    from library.primitives import empty, TABLES, validate
    from library.codec import decode, state_digest
    state=empty();state['revision']=meta['global_revision'];state['ledger']=decode(meta['ledger'])
    for kind in TABLES:
        for row in rows.get(kind,[]):
            envelope=decode(row['payload']);record=envelope['record']
            if 'cover' in envelope:
                path=_media_path(source,media,'attachments',envelope['cover'])
                if not path.exists():path=source/'payloads'/envelope['cover']
                record['cover']=path.read_text()
            state[kind][row['id']]=record
    validate(state)
    return state_digest(state)


def _verify_legacy_manifest(source: Path, files: dict[str,Any], rows: dict[str,list[dict[str,Any]]],
                            meta: dict[str,Any], semantic: str, media: dict[str,Any]) -> None:
    path=source/'recovery-manifest.yaml'
    if not path.exists():return
    from library.pg_import_records import load
    old=load(path)
    if (old.get('schema')!=1 or old.get('authority_id')!=meta['authority_id']
            or old.get('revision')!=meta['global_revision'] or old.get('semantic_sha256')!=semantic):
        raise LibraryError('Historical recovery identity or semantics differs')
    expected=set()
    for relative,entry in old.get('files',{}).items():
        current=source/'files'/relative
        if entry.get('absent'):
            if current.exists():raise LibraryError('Historically absent source is present')
        elif entry.get('directory'):
            if not current.is_dir():raise LibraryError('Historical source directory is absent')
        else:
            key='files/'+relative;expected.add(key)
            if key not in files or files[key]['sha256']!=entry.get('sha256') or files[key]['bytes']!=entry.get('bytes'):
                raise LibraryError('Historical component bytes differ')
    if {key for key in files if key.startswith('files/')}!=expected:
        raise LibraryError('Historical component inventory differs')
    for relative,expected_hash in old.get('collector_files',{}).items():
        if files.get('collector/'+relative,{}).get('sha256')!=expected_hash:raise LibraryError('Historical collector bytes differ')
    if old.get('attachments',[])!=sorted(row['digest'] for row in rows.get('attachments',[])):
        raise LibraryError('Historical attachment inventory differs')
    for namespace,binding in old.get('external_media',{}).items():
        if media.get(namespace,{}).get('files')!=binding['files']:
            raise LibraryError('Historical external media binding is absent or differs')
    if old.get('activity') is None and (source/'activity.sqlite').exists():
        raise LibraryError('Historically absent Activity is present')


if __name__=='__main__':
    raise SystemExit(main())
