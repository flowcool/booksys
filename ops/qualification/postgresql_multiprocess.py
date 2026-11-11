"""Public catalogue/task multi-process proof. Isolated database only.

The first process pauses inside its completion transaction. Two independent
processes must commit before it resumes. Retain all schemas and private state.
"""
from datetime import datetime,timezone
import hashlib,json,multiprocessing as mp,os,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from postgres_fixture import create_store
from library.primitives import empty
from library.store import Store,Change,PreparedResult
from library.stage_tasks import StageTasks,Obligation
class Retain:
 def addCleanup(self,*args):pass
root=Path(sys.argv[1])
if not root.is_absolute() or not root.is_relative_to('/data') or any(p.is_symlink() for p in (root,*root.parents)):
 raise SystemExit('Use a new isolated private root under /data')
root.mkdir(parents=True,mode=0o700,exist_ok=False)
store=create_store(Retain(),root/'state',empty(),authority='t5-multiprocess')
state=empty();assets=[];source_hashes={}
(root/'inputs').mkdir()
for index in range(3):
 raw=('Synthetic independent file '+str(index)).encode();path=root/'inputs'/('file-'+str(index)+'.cbz');path.write_bytes(raw)
 aid=hashlib.sha256(raw).hexdigest();oid='origin-'+str(index);assets.append(aid);source_hashes[path.name]=aid
 state['assets'][aid]={'sha256':aid,'size':len(raw),'format':'cbz','origins':[oid],'status':'validated','embedded':{}}
 state['origins'][oid]={'source':hashlib.sha256(('source-'+str(index)).encode()).hexdigest(),'pid':index+1,'fid':index+1,'path':path.name,'sha256':aid,'availability':'validated'}
store.commit(Change(upserts={'assets':state['assets'],'origins':state['origins']}),expected_revision=0)
now=datetime(2026,11,11,tzinfo=timezone.utc)
tasks=StageTasks(store)
active=[]
for index,queue in enumerate(('metadata','copies','routing')):
 task=tasks.discover(Obligation(queue,assets[index],'input','method'),now=now)
 active.append(tasks.start(task.key,task.generation,now=now,resources=True))
context=mp.get_context('fork');entered=context.Event();release=context.Event();results=context.Queue()
def worker(index):
 from library.catalog import Catalog
 current=Store(root/'state',authority=store.authority);owned=StageTasks(current);task=active[index]
 catalog=Catalog(root/'state',store=current)
 before=catalog.result_set('identify',{'assets':[assets[index]]})
 before['assets'][assets[index]]['embedded']={'Title':'File '+str(index)}
 prepared=catalog.prepare(before)
 original=Store._refresh_rows
 def hold(self,db,affected):
  original(self,db,affected)
  if index==0:
   entered.set()
   if not release.wait(8):raise RuntimeError('Independent peer did not progress')
 Store._refresh_rows=hold
 start=time.monotonic()
 try:
  outcome=owned.complete(task.key,task.generation,now=now,
   prepared=prepared,
   successors=[Obligation(('ocr','collection','intake')[index],'successor-'+str(index),'input','method')])
  results.put({'worker':index,'pid':os.getpid(),'status':outcome.status,'seconds':time.monotonic()-start})
 except BaseException as exc:results.put({'worker':index,'error':type(exc).__name__,'detail':str(exc)})
first=context.Process(target=worker,args=(0,));first.start()
if not entered.wait(8):raise RuntimeError('Held public completion did not enter: '+str(results.get(timeout=1)))
peers=[context.Process(target=worker,args=(index,)) for index in (1,2)]
for peer in peers:peer.start()
observed=[]
try:
 for _ in peers:observed.append(results.get(timeout=5))
 if any(row.get('status')!='done' for row in observed):raise RuntimeError('Independent progress failed')
 if not first.is_alive():raise RuntimeError('First transaction did not remain open')
finally:
 release.set()
for process in (first,*peers):process.join(10)
observed.append(results.get(timeout=5))
if any(process.exitcode!=0 for process in (first,*peers)):raise RuntimeError('Process failure')
for index,task in enumerate(active):
 if tasks.get(task.key).status!='done' or store.get('assets',assets[index])['embedded']!={'Title':'File '+str(index)}:raise RuntimeError('Missing result/completion')
if any(tasks.select(queue,now=now).eligible!=1 for queue in ('ocr','collection','intake')):raise RuntimeError('Missing successor')
(root/'report.json').write_text(json.dumps({'result':'PASS','application_sha256':__import__('library.authority',fromlist=['code_digest']).code_digest(),'python':sys.version,'schema':store._database.schema,'target':__import__('library.pg_import',fromlist=['target_identity']).target_identity(store._database),'workers':observed,'source_hashes':source_hashes,'all_results_and_successors':True},indent=2)+'\n')
print('PASS real independent processes commit public task results and successors while first transaction is open')
