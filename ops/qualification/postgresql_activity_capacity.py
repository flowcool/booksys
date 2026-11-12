"""Representative retained Activity DB+WAL import; synthetic private input only."""
from pathlib import Path
import hashlib,json,os,resource,shutil,sqlite3,sys,time,uuid
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
source_fixture=__import__('test_pg_import',fromlist=['source_fixture']).source_fixture
from library.pg_import import target_identity,prepare_manifest,import_snapshot,compare
from library.postgres import Database
from library.activity_store import ActivityStore
root=Path(sys.argv[1])
if not root.is_absolute() or not root.is_relative_to('/data') or any(p.is_symlink() for p in (root,*root.parents)):
 raise SystemExit('Use a new isolated private root under /data')
root.mkdir(parents=True,mode=0o700,exist_ok=False)
source=source_fixture(root/'source')
build=root/'activity-build.sqlite'
connection=sqlite3.connect(build)
connection.execute('PRAGMA journal_mode=WAL');connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('CREATE TABLE meta(singleton,schema,authority_id,history_started_at,gap_before)')
connection.execute("INSERT INTO meta VALUES(1,1,'capacity-activity',1700000000.25,1699999999.5)")
connection.execute('CREATE TABLE producers(stage,producer_scope,session,observed_at,progress_at,state,operation_code,reason_code,payload)')
connection.execute('CREATE TABLE events(id,at,stage,session,code,payload)')
producer={'stage':'local','producer_scope':'controller','session':'capacity-session','observed_at':1700000001.125,'progress_at':1700000000.75,'state':'waiting','operation':'idle','reason':'scheduled','title':'Retained producer','item_ref':''}
connection.execute('INSERT INTO producers VALUES(?,?,?,?,?,?,?,?,?)',('local','controller','capacity-session',1700000001.125,1700000000.75,'waiting','idle','scheduled',json.dumps(producer)))
count=16384;checksum=hashlib.sha256()
for index in range(count):
 payload=json.dumps({'title':'Synthetic retained payload '+'x'*6000,'position':index,'stage':'local','session':'capacity-session','operation':'idle'})
 values=(f'event-{index:08}',1700000000.25+index/1000,'local','capacity-session','idle',payload)
 connection.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',values)
 checksum.update(json.dumps(values,separators=(',',':')).encode()+b'\n')
connection.commit()
for suffix in ('','-wal'):
 shutil.copyfile(str(build)+suffix,str(source/'activity.sqlite')+suffix)
connection.close()
inputs={path.name:{'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in source.iterdir() if path.is_file()}
start=time.monotonic();manifest=prepare_manifest(source,candidate=os.environ['BOOKSYS_CANDIDATE'])
manifest_seconds=time.monotonic()-start
(root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
database=Database(Path(os.environ['BOOKSYS_TEST_PG_DSN_FILE']).read_text().strip(),'capacity_'+uuid.uuid4().hex)
start=time.monotonic();import_snapshot(manifest,root/'target',database=database);import_seconds=time.monotonic()-start
start=time.monotonic();report=compare(manifest,root/'target',database=database);compare_seconds=time.monotonic()-start
if report['status']!='equal':raise RuntimeError('Semantic import mismatch')
activity=ActivityStore(root/'target',authority='capacity-activity',database=database)
if activity.producers()!={'local:controller':producer}:raise RuntimeError('Producer fields changed')
observed=hashlib.sha256();after=None;total=0
while True:
 page=activity.events(after=after,limit=200)
 for item in page['items']:
  values=(item['id'],item['at'],item['stage'],'capacity-session','idle',json.dumps({'title':'Synthetic retained payload '+'x'*6000,'position':total,'stage':'local','session':'capacity-session','operation':'idle'}))
  # Check exact public payload as well as indexed identity/time.
  if item['position']!=total or item['title']!='Synthetic retained payload '+'x'*6000:raise RuntimeError('Event payload changed')
  observed.update(json.dumps(values,separators=(',',':')).encode()+b'\n');total+=1
 if page['cursor'] is None:break
 after=page['cursor']
if total!=count or observed.hexdigest()!=checksum.hexdigest():raise RuntimeError('Event history changed')
for name,identity in inputs.items():
 if hashlib.sha256((source/name).read_bytes()).hexdigest()!=identity['sha256']:raise RuntimeError('Frozen input changed')
(root/'result.json').write_text(json.dumps({'result':'PASS','candidate':os.environ['BOOKSYS_CANDIDATE'],'python':sys.version,'application_sha256':manifest['code_sha256'],'schema':database.schema,'target':target_identity(database),'inputs':inputs,'events':count,'event_semantic_sha256':checksum.hexdigest(),'manifest_seconds':manifest_seconds,'import_seconds':import_seconds,'compare_seconds':compare_seconds,'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'source_unchanged':True,'public_event_count':total},indent=2)+'\n')
print('PASS representative Activity DB+WAL import, exact producer and public event history')
