"""Prepare retained synthetic fresh Compose PostgreSQL inputs. No Docker calls."""
import argparse,json,os,re,secrets,sys
from pathlib import Path
import yaml
from psycopg.conninfo import make_conninfo
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root',type=Path,required=True)
parser.add_argument('--images',type=Path,required=True)
parser.add_argument('--cgroup-parent',required=True)
args=parser.parse_args()
if not re.fullmatch(r'booksysprfpwqualification[a-z0-9]*\.slice',args.cgroup_parent):
 raise SystemExit('Select a root-level isolated qualification slice without hyphens')
if not args.root.is_absolute() or not args.root.is_relative_to('/data') or any(p.is_symlink() for p in (args.root,*args.root.parents)):
 raise SystemExit('Use a new absolute nonsymlink private root under /data')
root=args.root
images=json.loads(args.images.read_text())
root.mkdir(mode=0o700,parents=True,exist_ok=False)
compose=(Path(__file__).resolve().parents[2]/'compose.stack.yaml').read_text()
variables=set(re.findall(r'\$\{(BOOKSYS_[A-Z0-9_]+)',compose))
env={key:str(root/'unused'/key.lower()) for key in variables}
for name in ('pg','socket','secrets','config','launcher-db','inputs/downloads','jobs','library/state','collector','ipc','reader-ipc','files/work/reader-jobs','managed','export'):
 (root/name).mkdir(parents=True,exist_ok=True)
# Parent permits postgres uid traversal; private secrets stay 0600/0640.
root.chmod(0o755)
(root/'pg').chmod(0o700)
(root/'secrets').chmod(0o711)
(root/'socket').chmod(0o770)
password=secrets.token_hex(32)
(root/'secrets/password').write_text(password)
(root/'secrets/password').chmod(0o640)
for name in ('app-password','launcher-password'):
 (root/'secrets'/name).write_text(secrets.token_hex(32));(root/'secrets'/name).chmod(0o600)
for role,password_file in [('migration','password'),('app','app-password'),('launcher','launcher-password')]:
 (root/'secrets'/(role+'.dsn')).write_text(make_conninfo(host=str(root/'socket'),dbname='booksys',user='booksys_'+role,password=(root/'secrets'/password_file).read_text()))
 (root/'secrets'/(role+'.dsn')).chmod(0o600)
env.update(BOOKSYS_PROJECT='booksys-prfpw-qualification-'+secrets.token_hex(4),BOOKSYS_NETWORK='booksys-prfpw-qualification-network-'+secrets.token_hex(4),BOOKSYS_POSTGRES_ROOT=str(root/'pg'),BOOKSYS_POSTGRES_SOCKET_ROOT=str(root/'socket'),BOOKSYS_POSTGRES_PASSWORD_FILE=str(root/'secrets/password'),BOOKSYS_APP_DSN_FILE=str(root/'secrets/app.dsn'),BOOKSYS_LAUNCHER_DSN_FILE=str(root/'secrets/launcher.dsn'),BOOKSYS_POSTGRES_IMAGE='postgres:18.6-bookworm@sha256:f1ea2a1a4fb7b68d4836be1fdcb8a6466a89b072158bf9d97663600538c294c5',BOOKSYS_LOCAL_ROOT=str(root/'library'),BOOKSYS_COLLECTOR_ROOT=str(root/'collector'),BOOKSYS_STACK_CONFIG=str(root/'config'),BOOKSYS_NAS_FILES_ROOT=str(root/'files'),BOOKSYS_NAS_LIBRARY_ROOT=str(root/'managed'),BOOKSYS_NAS_LIBRARY_EXPORT_ROOT=str(root/'export'),BOOKSYS_IPC_ROOT=str(root/'ipc'),BOOKSYS_READER_IPC_ROOT=str(root/'reader-ipc'),BOOKSYS_PUBLIC_HOST='127.0.0.1:18768',BOOKSYS_BIND_ADDRESS='127.0.0.1',BOOKSYS_PYLOAD_PORT='18000',BOOKSYS_WEB_BIND_ADDRESS='127.0.0.1',BOOKSYS_WEB_PORT='18768',BOOKSYS_PUBLIC_ORIGIN='',BOOKSYS_CGROUP_PARENT=args.cgroup_parent,BOOKSYS_DOCKER_GID=str(Path('/run/docker.sock').stat().st_gid))
env['BOOKSYS_BYPARR_IMAGE']=images['images']['app']['id']
env['BOOKSYS_PYLOAD_IMAGE']=images['images']['app']['id']
for role,key in [('app','BOOKSYS_APP_IMAGE'),('reader','BOOKSYS_READER_IMAGE'),('launcher','BOOKSYS_LAUNCHER_IMAGE')]:env[key]=images['images'][role]['id']
(root/'qualification.env').write_text('\n'.join(key+'='+value for key,value in sorted(env.items()))+'\n')
(root/'qualification.env').chmod(0o600)
(root/'preparation.json').write_text(json.dumps({'candidate':images['commit'],'project':env['BOOKSYS_PROJECT'],'network':env['BOOKSYS_NETWORK'],'root':str(root),'ownership_required':{'pg':'999:999','socket':'999:1000','secrets/password':'999:1000'}},indent=2)+'\n')
print('Prepared private synthetic Compose inputs; no runtime mutation')

# This overlay qualifies the actual restricted launcher with synthetic local
# inputs. NAS identity qualification remains a separate retained gate.
launcher={'image':env['BOOKSYS_LAUNCHER_IMAGE'],'user':'1000:1000','network_mode':'none',
 'read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],
 'group_add':[env['BOOKSYS_DOCKER_GID']],'mem_limit':'128m','memswap_limit':'128m',
 'pids_limit':48,'cpus':'1','cgroup_parent':args.cgroup_parent,'restart':'no',
 'volumes':[str(root/'socket')+':'+str(root/'socket'),
 str(root/'secrets/launcher.dsn')+':'+str(root/'secrets/launcher.dsn')+':ro',
 str(root/'launcher-db')+':/launcher-db:ro',str(root/'library/state')+':/state',
 str(root/'reader-ipc')+':/reader-ipc',str(root/'inputs')+':/inputs:ro',
 str(root/'jobs')+':'+str(root/'jobs'),str(root/'config')+':/config:ro',
 '/run/docker.sock:/var/run/docker.sock'],
 'environment':{'BOOKSYS_LAUNCHER_DATABASE_DIR':'/launcher-db',
 'BOOKSYS_COMPOSE_PROJECT':env['BOOKSYS_PROJECT'],
 'BOOKSYS_RETAINED_SOURCES_FILE':'/config/retained-sources.json'},
 'command':['-m','library.reader_service','--socket','/reader-ipc/reader.sock',
 '--inputs','/inputs','--jobs',str(root/'jobs'),'--state','/state','--image',
 env['BOOKSYS_READER_IMAGE'],'--job-uid','1000','--cgroup-parent',args.cgroup_parent]}
(root/'launcher.compose.yaml').write_text(yaml.safe_dump({'name':env['BOOKSYS_PROJECT'],
 'services':{'reader-launcher':launcher}}))
(root/'config/retained-sources.json').write_text('{}\n')
