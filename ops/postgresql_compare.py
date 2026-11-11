"""Independent frozen-source comparison before PostgreSQL selection.

Direct source/target rows and public collector readers. No importer projection
helpers, native effects, source database opens or target writes. SQLite is used
only on new private main/WAL copies retained under the configured temp root.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import psycopg
from psycopg import sql
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from library.operational_state import CollectorState, NativeHistory
from library.postgres import Database


def catalogue(args, source_db, source_policy):
    source=sqlite3.connect(source_db.as_uri()+'?mode=ro',uri=True)
    policy=yaml.safe_load(source_policy.read_text())['tasks']
    source_tables=[r[0] for r in source.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    def value(x):
        if isinstance(x,(bytes,memoryview)): return {'bytes_hex':bytes(x).hex()}
        return x

    def canonical(columns,row,table):
        v=dict(zip(columns,row))
        for key in ('payload','ledger','required_runtime'):
            if key in v and isinstance(v[key],str):
                try: v[key]=json.loads(v[key])
                except json.JSONDecodeError: pass
        if table=='stage_tasks':
            original=policy.get(v['key'])
            if original is not None:
                v.update(stage=original['stage'],status=original['status'],age=original['first_eligible_at'],due=original['due_at'],blocked=original['blocked'],payload=original)
            if isinstance(v['payload'],dict) and v['payload'].get('native_attempt','absent') is None:
                v['payload']={k:x for k,x in v['payload'].items() if k!='native_attempt'}
        return json.dumps({k:value(x) for k,x in v.items()},sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

    reports={}
    with psycopg.connect(args.dsn_file.read_text().strip(),autocommit=True) as target:
        target.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        target_identity=target.execute('SELECT current_database(),system_identifier::text,current_user,current_setting(\'server_version_num\') FROM pg_control_system()').fetchone()
        for table in source_tables:
            if table=='stage_schema': continue
            columns=[r[1] for r in source.execute('PRAGMA table_info("'+table+'")')]
            if table=='meta': columns=['authority_id','global_revision','ledger','case_scope_revision']
            source_rows=source.execute('SELECT '+','.join('"'+c+'"' for c in columns)+' FROM "'+table+'"').fetchall()
            query=sql.SQL('SELECT {} FROM {}.{}').format(sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(args.schema),sql.Identifier(table))
            target_rows=target.execute(query).fetchall()
            if table=='stage_tasks':
                indexed={row[columns.index('key')] for row in source_rows}
                for key, task in policy.items():
                    if key not in indexed:
                        record={'key':key,'stage':task['stage'],'status':task.get('status','pending'),
                                'age':task['first_eligible_at'],'due':task['due_at'],
                                'blocked':task.get('blocked',''),'payload':json.dumps(task)}
                        if set(columns)-record.keys():
                            raise SystemExit('Unknown source task index columns')
                        source_rows.append(tuple(record[c] for c in columns))
            # Source policy overrides apply only to historical SQL indexes. The
            # target must already contain the canonical policy; never repair it here.
            expected=sorted(canonical(columns,row,table) for row in source_rows)
            actual=[]
            for row in target_rows:
                if table=='stage_tasks':
                    saved=policy
                    policy={}
                    try: actual.append(canonical(columns,row,table))
                    finally: policy=saved
                else: actual.append(canonical(columns,row,table))
            actual.sort()
            digest=lambda rows:hashlib.sha256(('\n'.join(rows)+'\n').encode()).hexdigest()
            reports[table]={'source_rows':len(expected),'target_rows':len(actual),'source_sha256':digest(expected),'target_sha256':digest(actual),'equal':expected==actual}
        target.execute('ROLLBACK')
    source.close()
    report={'helper_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'target_identity':dict(zip(('database','system_identifier','role','server_version'),target_identity)),'source_files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (source_db,source_db.with_name(source_db.name+'-wal'),source_policy) if p.is_file()},'candidate':args.candidate,'source_db':str(source_db),'source_policy':str(source_policy),'target_schema':args.schema,'method':'Direct SQLite and PostgreSQL column reads; no application/importer conversion helpers; YAML overrides source policy indexes only. New absent native_attempt normalized to omitted. Meta compares preserved identity/revision/ledger/scope. Transport stage_schema excluded.','tables':reports,'equal':all(r['equal'] for r in reports.values())}
    return report


def activity(args, source_db):
    c=sqlite3.connect(Path(source_db).as_uri()+'?mode=ro',uri=True)
    report={}
    with psycopg.connect(Path(args.dsn_file).read_text().strip(),autocommit=True) as target:
     target.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
     identity=target.execute('SELECT current_database(),system_identifier::text,current_user FROM pg_control_system()').fetchone()
     for old,new in (('meta','activity_meta'),('producers','activity_producers'),('events','activity_events')):
      cols=[r[1] for r in c.execute('PRAGMA table_info("'+old+'")')]
      if not cols:raise SystemExit('Required Activity source table absent')
      source=c.execute('SELECT '+','.join('"'+k+'"' for k in cols)+' FROM "'+old+'"').fetchall()
      rows=target.execute(sql.SQL('SELECT {} FROM {}.{}').format(sql.SQL(',').join(map(sql.Identifier,cols)),sql.Identifier(args.schema),sql.Identifier(new))).fetchall()
      def normalized(values):
       result=[]
       for row in values:
        item=dict(zip(cols,row))
        if 'payload' in item:item['payload']=json.loads(item['payload'])
        result.append(json.dumps(item,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False))
       return sorted(result)
      expected,actual=normalized(source),normalized(rows)
      digest=lambda v:hashlib.sha256(('\n'.join(v)+'\n').encode()).hexdigest()
      report[new]={'source_rows':len(expected),'target_rows':len(actual),'source_sha256':digest(expected),'target_sha256':digest(actual),'equal':expected==actual}
     target.execute('ROLLBACK')
    c.close()
    value={'candidate':args.candidate,'helper_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'source_db':str(source_db),'target_schema':args.schema,'target_identity':identity,'tables':report,'equal':all(v['equal'] for v in report.values()),'method':'Direct read-only source/target SQL; no Activity report, pruning, normalization or importer helpers. JSON payload serialization only is canonicalized.'}
    return value


def collector(args, source):
    database=Database(args.dsn_file.read_text().strip(),args.schema)
    collector=CollectorState(args.collector,database=database)
    native=NativeHistory(args.collector,database=database)
    def typed(v):
     if isinstance(v,dict):
      return ['dict',sorted([[typed(k),typed(x)] for k,x in v.items()],key=lambda x:json.dumps(x[0],sort_keys=True))]
     if isinstance(v,(list,tuple)):return [type(v).__name__,[typed(x) for x in v]]
     if isinstance(v,(set,frozenset)):return [type(v).__name__,sorted([typed(x) for x in v],key=lambda x:json.dumps(x,sort_keys=True))]
     return [type(v).__name__,repr(v)]
    def stamp(v):return hashlib.sha256(json.dumps(typed(v),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    reports={}
    for name,reader in (('state.yaml',collector.captured),('monitor.yaml',collector.monitor),('fiches.yaml',collector.fiches),('backfill.yaml',collector.backfill),('mirror-routing.yaml',native.routing),('package-retirement.yaml',native.retirements)):
     raw=(source/name).read_bytes();expected=yaml.safe_load(raw)
     start=time.monotonic();actual=reader();duration=time.monotonic()-start
     reports[name]={'source_file_sha256':hashlib.sha256(raw).hexdigest(),'source_semantic_sha256':stamp(expected),'target_semantic_sha256':stamp(actual),'equal':typed(expected)==typed(actual),'duration_seconds':duration}
    v={'candidate':args.candidate,'target_schema':database.schema,'helper_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'method':'Direct source YAML comparison with public canonical PostgreSQL readers; exact recursive Python scalar/container types; no importer projection helpers, native effects or writes.','reports':reports,'equal':all(x['equal'] for x in reports.values())}
    return v


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('frozen','dsn-file','collector','report'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--schema',required=True)
    parser.add_argument('--candidate',required=True)
    args=parser.parse_args()
    for path in (args.frozen,args.dsn_file,args.collector,args.report):
        if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):
            raise SystemExit('Use absolute nonsymlink operation paths')
    if args.report.exists() or args.report.is_relative_to(args.frozen):
        raise SystemExit('Use a new report outside frozen inputs')
    os.umask(0o077)
    private=Path(tempfile.mkdtemp(prefix='booksys-prfpw-independent-'))
    source_files={}
    for name in ('catalogue.sqlite','activity.sqlite'):
        source=args.frozen/name
        if not source.is_file():
            raise SystemExit('Required frozen source database absent: '+name)
        for suffix in ('','-wal'):
            item=source.with_name(name+suffix)
            if item.exists():
                before=hashlib.sha256(item.read_bytes()).hexdigest()
                shutil.copyfile(item,private/item.name)
                if hashlib.sha256(item.read_bytes()).hexdigest()!=before:
                    raise SystemExit('Frozen input changed')
                source_files[str(item)]=before
    reports={'catalogue':catalogue(args,private/'catalogue.sqlite',args.frozen/'files/task-policy.yaml'),
             'activity':activity(args,private/'activity.sqlite'),
             'collector':collector(args,args.frozen/'collector')}
    for name,expected in source_files.items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=expected:
            raise SystemExit('Frozen input changed during comparison')
    result={'candidate':args.candidate,'helper_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'private_sqlite_copies':str(private),'frozen_source_hashes':source_files,
            'reports':reports,'equal':all(r['equal'] for r in reports.values()),
            'scope':'Independent read-only exact frozen catalogue, Activity and six public collector readers. Not runtime activation.'}
    with args.report.open('x') as stream:
        json.dump(result,stream,indent=2);stream.write('\n')
    print(json.dumps({'equal':result['equal'],'catalogue_tables':len(reports['catalogue']['tables']),
                      'activity_tables':len(reports['activity']['tables']),'collector_families':len(reports['collector']['reports'])}))
    raise SystemExit(0 if result['equal'] else 1)


if __name__=='__main__':main()
