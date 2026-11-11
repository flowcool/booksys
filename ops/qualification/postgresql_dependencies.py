"""Root-owned synthetic dependency network and actual diagnostic IPC proof."""
import argparse,json,os,subprocess,sys,time
from datetime import datetime,timezone
import uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from library.postgres import Database
from library.operational_state import ComposeDiagnostics
from library.reader_client import available
from library.compose_diagnostics import RemoteSource
parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--images',type=Path,required=True);parser.add_argument('--resume',action='store_true');parser.add_argument('--run-id',default='first');args=parser.parse_args();root=args.root
if not root.is_absolute() or not root.is_relative_to('/data') or any(p.is_symlink() for p in (root,*root.parents)):
 raise SystemExit('Use the prepared private isolated root under /data')
binding=json.loads((root/'preparation.json').read_text());images=json.loads(args.images.read_text());project=binding['project'];network=binding['network']
def run(*argv: str) -> str:return subprocess.check_output(argv,text=True).strip()
if not __import__('re').fullmatch('[a-z0-9]{1,16}',args.run_id):raise SystemExit('Invalid isolated run ID')
containers: list[str]=[];sources: dict[str,dict[str,str]]={};evidence=root/('mixed-project-'+args.run_id);evidence.mkdir(exist_ok=args.resume)
try:
 for role in ('pyload','byparr'):
  name=project+'-'+args.run_id+'-synthetic-'+role
  event={'schema':1,'event_id':str(uuid.uuid4()),'at':datetime.now(timezone.utc).isoformat(),'service':'collector','stage':'collection','code':'waiting','reason':'idle','level':'INFO','refs':[]}
  script="import json,time;print("+repr(json.dumps(event))+",flush=True);time.sleep(600)"
  if args.resume:
   retained=json.loads((root/'config/retained-sources.json').read_text())[role]
   observed=json.loads(run('docker','inspect','--format','{{json .}}',retained['id']))
   if observed['Name']!='/'+name or observed['Config']['Labels'].get('com.docker.compose.project')!=retained['project'] or observed['Config']['Labels'].get('com.docker.compose.service')!=role:raise RuntimeError('Retained synthetic dependency identity differs')
   identity=retained['id']
  else:
   identity=run('docker','create','--name',name,'--network',network,'--network-alias',role,'--label','com.docker.compose.project='+project+'-retained','--label','com.docker.compose.service='+role,'--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--memory=64m','--memory-swap=64m','--cpus=.1','--pids-limit=16',images['images']['app']['id'],'-c',script)
  containers.append(identity);sources[role]={'id':identity,'project':project+'-retained','service':role};run('docker','start',identity)
 (root/'config/retained-sources.json').write_text(json.dumps(sources))
 # Launcher rereads fixed configuration on every observed/requested source.
 os.environ['BOOKSYS_READER_SOCKET']=str(root/'reader-ipc/reader.sock')
 # Actual launcher must already be started by root using launcher.compose.yaml.
 if not available():raise RuntimeError('Actual launcher IPC not ready')
 diagnostics=ComposeDiagnostics(Database.configured(root/'library/state'))
 deadline=time.monotonic()+12
 while True:
  observed=diagnostics.sources()
  if all(observed['sources'].get(role)==value['id'] for role,value in sources.items()):break
  if time.monotonic()>deadline:raise RuntimeError('Mixed-project observer generation missing')
  time.sleep(.2)
 now=time.time()
 for role,value in sources.items():
  source=RemoteSource(role,root/'library/state');source.refresh()
  if source.generation!=value['id']:raise RuntimeError('RemoteSource generation differs')
  result=source.scan(now-60,now,None,lines=100,byte_limit=65536,deadline=time.monotonic()+3)
  if not result.get('done') or result.get('lines',0)<1:raise RuntimeError('Actual fixed log IPC returned no coverage')
  (evidence/(role+('-resume' if args.resume else '')+'-logs.json')).write_text(json.dumps(result,indent=2)+'\n')
 # Retained DNS resolution and bounded TCP are tested by an inert app image.
 probe_name=project+'-'+args.run_id+'-dns-probe'
 probe_file=evidence/'dns-probe-id'
 probe_command=['-c',"import socket,json;print(json.dumps({name:socket.gethostbyname(name) for name in ('pyload','byparr')}))"]
 existing=subprocess.run(['docker','inspect','--format','{{json .}}',probe_name],capture_output=True,text=True)
 if existing.returncode==0:
  if not args.resume:raise RuntimeError('DNS probe already exists; explicit resume required')
  retained_probe=json.loads(existing.stdout);probe=retained_probe['Id']
  if (retained_probe['Name']!='/'+probe_name or retained_probe['Image']!=images['images']['app']['id']
      or retained_probe['Mounts'] or retained_probe['Config']['Cmd']!=probe_command
      or retained_probe['Config']['Labels'].get('booksys.qualification.dns')!=project
      or set(retained_probe['NetworkSettings']['Networks'])!={network}
      or probe_file.exists() and probe_file.read_text().strip()!=probe):
   raise RuntimeError('Retained DNS probe identity differs')
 else:
  if not ('no such object:' in existing.stderr.lower() or 'no such container:' in existing.stderr.lower()):
   raise RuntimeError('DNS probe lookup failed; absence unproved')
  if probe_file.exists():raise RuntimeError('Retained DNS probe disappeared')
  probe=run('docker','create','--name',probe_name,'--label','booksys.qualification.dns='+project,'--network',network,'--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',images['images']['app']['id'],*probe_command)
 probe_file.write_text(probe+'\n')
 containers.append(probe)
 state=json.loads(run('docker','inspect','--format','{{json .State}}',probe))
 if state['Status']=='created':run('docker','start',probe)
 if state['Status']=='created' or state['Running']:run('docker','wait',probe)
 logs=run('docker','logs',probe);(evidence/'dns.json').write_text(logs+'\n')
 if set(json.loads(logs))!={'pyload','byparr'}:raise RuntimeError('Synthetic dependency DNS failed')
 (evidence/('result-resume.json' if args.resume else 'result.json')).write_text(json.dumps({'result':'PASS','project':project,'network':network,'retained_sources':sources,'observer':observed,'probe':probe},indent=2)+'\n')
finally:
 for identity in containers:
  run('docker','stop','--time','1',identity)
  inspected=json.loads(run('docker','inspect','--format','{{json .}}',identity))
  (evidence/(identity+'-inspect.json')).write_text(json.dumps(inspected,indent=2)+'\n')
  (evidence/(identity+'-logs.txt')).write_text(run('docker','logs',identity)+'\n')
  if inspected['State']['Running']:raise RuntimeError('Synthetic dependency/probe stop unproved')
print('PASS exact retained dependency DNS, actual observer and fixed log IPC')
