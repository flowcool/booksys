"""Synthetic catalogue/task/source volume import, no live database reads."""
from pathlib import Path
from contextlib import closing
from dataclasses import asdict
from datetime import datetime,timezone
import hashlib,json,os,resource,shutil,sqlite3,sys,time,uuid,yaml
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
source_fixture=__import__('test_pg_import',fromlist=['source_fixture']).source_fixture
from library.pg_import import target_identity,prepare_manifest,import_snapshot,compare,select
from library.postgres import Database
from library.store import Store
from library.stage_tasks import StageTasks,Obligation
from library.stage_model import Task
from library.operational_state import CollectorState
from library.authority import code_digest
root=Path(sys.argv[1])
if not root.is_absolute() or not root.is_relative_to('/data') or any(p.is_symlink() for p in (root,*root.parents)):
 raise SystemExit('Use a new isolated private root under /data')
root.mkdir(parents=True,mode=0o700,exist_ok=False)
source=source_fixture(root/'source')
build=root/'catalogue-build.sqlite';shutil.copyfile(source/'catalogue.sqlite',build)
connection=sqlite3.connect(build);connection.execute('PRAGMA journal_mode=WAL');connection.execute('PRAGMA wal_autocheckpoint=0')
count=20000
for index in range(count):connection.execute('INSERT INTO works VALUES(?,?,?)',(f'volume-{index:06}',3,json.dumps({'record':{'title':f'Synthetic retained work {index:06} '+'x'*4000}})))
connection.commit()
for suffix in ('','-wal'):shutil.copyfile(str(build)+suffix,str(source/'catalogue.sqlite')+suffix)
connection.close()
items={f'source-{index:04}':{'title':f'Retained source {index}','downloads':{'fixture':{'urls':['https://fixture.invalid/'+str(index)]}}} for index in range(837)}
(source/'collector/state.yaml').write_text(yaml.safe_dump({'version':1,'items':items}))
stamp='2026-11-11T00:00:00+00:00';task_rows={}
for index in range(2262):
 obligation=Obligation(('collection','intake','ocr','metadata','copies','routing','retirement')[index%7],f'entity-{index}','retained-input','retained-method')
 task=Task(key=obligation.key,stage=obligation.stage,entity=obligation.entity,input_revision=obligation.input_revision,method=obligation.method,first_eligible_at=stamp,due_at=stamp)
 task_rows[task.key]=asdict(task)
(source/'files/task-policy.yaml').write_text(yaml.safe_dump({'schema':1,'sources':{},'tasks':task_rows}))
inputs={str(path.relative_to(source)):{'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in source.rglob('*') if path.is_file()}
start=time.monotonic();manifest=prepare_manifest(source,candidate=os.environ['BOOKSYS_CANDIDATE']);manifest_seconds=time.monotonic()-start
(root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
database=Database(Path(os.environ['BOOKSYS_TEST_PG_DSN_FILE']).read_text().strip(),'capacity_'+uuid.uuid4().hex)
phases=[];previous=time.monotonic()
def checkpoint(phase: str) -> None:
 global previous
 now=time.monotonic();phases.append({'phase':phase,'seconds_since_previous':now-previous});previous=now
start=time.monotonic();import_snapshot(manifest,root/'target',database=database,checkpoint=checkpoint);import_seconds=time.monotonic()-start
start=time.monotonic();report=compare(manifest,root/'target',database=database);compare_seconds=time.monotonic()-start
if report['status']!='equal':raise RuntimeError('Semantic import mismatch')
secret=root/'target/runtime.dsn';secret.write_text(database.dsn);secret.chmod(0o600)
config=root/'target/postgresql.json';config.write_text(json.dumps({'schema':database.schema,'dsn_file':str(secret)}));config.chmod(0o600)
select(manifest,root/'target',database=database)
store=Store(root/'target',authority='source-test',database=database)
collector=CollectorState(root/'collector',database=database)
# Exact source JSON values through the public catalogue-independent reader.
if collector.captured()!={'version':1,'items':items}:raise RuntimeError('Source data changed')
tasks=StageTasks(store)
for key,expected in task_rows.items():
 observed_task=tasks.get(key)
 if observed_task is None or asdict(observed_task)!=expected:raise RuntimeError('Task policy changed')
if store.get('works','volume-019999')!={'title':'Synthetic retained work 019999 '+'x'*4000}:raise RuntimeError('Catalogue payload changed')
for name,identity in inputs.items():
 if hashlib.sha256((source/name).read_bytes()).hexdigest()!=identity['sha256']:raise RuntimeError('Frozen input changed')
(root/'result.json').write_text(json.dumps({'result':'PASS','candidate':os.environ['BOOKSYS_CANDIDATE'],'application_sha256':code_digest(),'python':sys.version,'schema':database.schema,'target':target_identity(database),'inputs':inputs,'works':count+1,'captured_sources':len(items),'tasks':len(task_rows),'manifest_seconds':manifest_seconds,'import_seconds':import_seconds,'compare_seconds':compare_seconds,'phases':phases,'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'source_unchanged':True},indent=2)+'\n')
print('PASS representative catalogue volume, 837 sources and 2262 task policies imported and read back')
