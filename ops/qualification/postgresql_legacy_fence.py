"""Prepare/prove exact legacy file fencing on a synthetic local fixture.

Root applies immutable flags only to the four prepared files between actions.
No container, flag or production mutation is performed by this command.
"""
import argparse,hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('action',choices=('prepare','prove'))
parser.add_argument('--root',type=Path,required=True)
parser.add_argument('--current-state',type=Path,required=True)
args=parser.parse_args();root=args.root
if not root.is_absolute() or not root.is_relative_to('/data') or any(p.is_symlink() for p in (root,*root.parents)):
 raise SystemExit('Use a new synthetic root under /data')
current=args.current_state
if (not current.is_absolute() or not current.is_relative_to('/data/tmp')
        or any(p.is_symlink() for p in (current,*current.parents))):
 raise SystemExit('Only a prepared isolated qualification state is accepted')
prepared=current.parent.parent
binding=json.loads((prepared/'preparation.json').read_text())
if current!=prepared/'library/state' or Path(binding['root'])!=prepared or not binding['project'].startswith('booksys-prfpw-qualification-'):
 raise SystemExit('Current state differs from the prepared isolated target')
sys.path.insert(0,str(ROOT))
from library.postgres import Database
from library.pg_import import target_identity
import yaml
selector=yaml.safe_load((current/'storage-authority.yaml').read_text())
database=Database.configured(current)
if selector.get('authority_id')!='t5-isolated-reader' or database.schema!='qualification':
 raise SystemExit('Only the exact synthetic authority can receive qualification writes')
with database.connection() as db:
 if db.execute('SELECT authority_id FROM meta').fetchone()!=('t5-isolated-reader',):
  raise SystemExit('Synthetic target authority differs')
database_identity=target_identity(database)
legacy=root/'legacy';state=root/'state'
files=('state.yaml','monitor.yaml','backfill.yaml','fiches.yaml')
if args.action=='prepare':
 root.mkdir(mode=0o700,parents=True,exist_ok=False);legacy.mkdir();state.mkdir()
 paths=subprocess.check_output(['git','ls-tree','-r','--name-only','fb69c201','booksys_core'],cwd=ROOT,text=True).splitlines()
 for name in paths:
  if name.endswith('.py'):
   target=legacy/name;target.parent.mkdir(parents=True,exist_ok=True)
   target.write_bytes(subprocess.check_output(['git','show','fb69c201:'+name],cwd=ROOT))
 for name in files:(state/name).write_text('version: 1\nitems: {}\n' if name!='monitor.yaml' else 'version: 1\ncategories: {}\nsubmissions: {}\n')
 # A selected PostgreSQL authority does not interlock these historical writers.
 (state/'storage-authority.yaml').write_bytes((args.current_state/'storage-authority.yaml').read_bytes())
 (root/'before.json').write_text(json.dumps({name:hashlib.sha256((state/name).read_bytes()).hexdigest() for name in files},indent=2)+'\n')
 (root/'target.json').write_text(json.dumps({'state':str(current),'preparation':binding,'database_identity':database_identity,'postgresql_config_sha256':hashlib.sha256((current/'postgresql.json').read_bytes()).hexdigest()},indent=2)+'\n')
 print('Prepared exact fb69c201 legacy modules and four synthetic retired authority files')
else:
 target=json.loads((root/'target.json').read_text())
 if target!={'state':str(current),'preparation':binding,'database_identity':database_identity,'postgresql_config_sha256':hashlib.sha256((current/'postgresql.json').read_bytes()).hexdigest()}:
  raise SystemExit('Retained isolated target binding changed')
 legacy_probe="""from pathlib import Path
import sys
sys.path[:]=[sys.argv[1]]+[entry for entry in sys.path if entry and entry!=sys.argv[3]]
from booksys_core.files import save
from booksys_core.scheduler import ScheduledCalls
import booksys_core.files
assert Path(booksys_core.files.__file__).is_relative_to(Path(sys.argv[1]))
state=Path(sys.argv[2]);hits=[]
for name in ('state.yaml','monitor.yaml','backfill.yaml','fiches.yaml'):
 try:save(state/name,{'version':1,'items':{'forbidden':'changed'}})
 except PermissionError:pass
 else:raise RuntimeError('Legacy save did not refuse '+name)
try:ScheduledCalls(state,lambda url:'unused').call('synthetic-reference',lambda:hits.append('external-effect'))
except PermissionError:pass
else:raise RuntimeError('Legacy scheduled call did not refuse')
assert not hits
print('PASS exact legacy modules refused before effect')
"""
 legacy_result=subprocess.run([sys.executable,'-c',legacy_probe,str(legacy),str(state),str(ROOT)],cwd=legacy,capture_output=True,text=True)
 (root/'legacy-probe.log').write_text(legacy_result.stdout+legacy_result.stderr)
 if legacy_result.returncode:raise RuntimeError('Exact legacy subprocess proof failed')
 hits: list[str]=[]
 before=json.loads((root/'before.json').read_text())
 if before!={name:hashlib.sha256((state/name).read_bytes()).hexdigest() for name in files}:raise RuntimeError('Legacy source changed')
 # New code runs in a fresh interpreter; never mix old/new module caches.
 result=subprocess.run([sys.executable,'-c',"from pathlib import Path; from library.catalog import Catalog; from library.store import Change; import sys; c=Catalog(Path(sys.argv[1])); s=c.backend(); s.commit(Change(upserts={'works':{'legacy-fence-proof':{'title':'Current PostgreSQL write'}}}),expected_revision=s.revision); assert c.get('works','legacy-fence-proof')=={'title':'Current PostgreSQL write'}",str(args.current_state)],cwd=ROOT,capture_output=True,text=True)
 if result.returncode:raise RuntimeError('Current PostgreSQL public write/readback failed: '+result.stderr)
 (root/'result.json').write_text(json.dumps({'result':'PASS','legacy_commit':'fb69c201','fenced_files':before,'scheduled_effect_calls':hits,'current_pg_public_write':True,'limits':'Exact synthetic file replacement fence; deliberate root flag removal and unsupported binary bypass remain outside supported startup.'},indent=2)+'\n')
 print('PASS old exact saves and scheduled effect refuse; current PostgreSQL write/readback succeeds')
