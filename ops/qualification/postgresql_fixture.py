"""Initialize or exercise a retained synthetic PostgreSQL/reader fixture.

Root executes this against the freshly bootstrapped isolated Compose target only.
No production paths, native downloader calls or hoster requests are accepted.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from library.authority import code_digest
from library.catalog import Catalog,empty
from library.decoder_ownership import DecoderOwnership
from library.postgres import Database
from library.primitives import atomic_yaml
from library.reader_client import execute
from library.stage_tasks import StageTasks,Obligation
from library.stage_resources import Resources,Demand
from library.store import Store,Change
from ops.postgres_roles import provision,private


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('initialize','reader','configure-launcher','qualify-code'))
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--cgroup-parent')
    parser.add_argument('--expected-code-sha256')
    args=parser.parse_args();root=args.root
    if not root.is_absolute() or not root.is_relative_to('/data') or any(p.is_symlink() for p in (root,*root.parents)):
        raise SystemExit('Use the retained isolated fixture root under /data')
    binding=json.loads((root/'preparation.json').read_text())
    state=root/'library/state';schema='qualification'
    if args.action=='qualify-code':
        import re,yaml
        from library.pg_import import target_identity
        if not args.expected_code_sha256 or not re.fullmatch('[0-9a-f]{64}',args.expected_code_sha256):
            raise SystemExit('Bind the exact previously qualified synthetic code digest')
        path=state/'storage-authority.yaml';value=yaml.safe_load(path.read_text())
        database=Database.configured(state)
        if (binding['root']!=str(root) or not binding['project'].startswith('booksys-prfpw-qualification-')
                or value.get('authority_id')!='t5-isolated-reader' or database.schema!=schema
                or value.get('code_sha256') not in (args.expected_code_sha256,code_digest())):
            raise SystemExit('Only the exact synthetic authority can advance qualification code')
        with database.connection() as db:
            if db.execute('SELECT authority_id FROM meta').fetchone()!=('t5-isolated-reader',):
                raise SystemExit('Synthetic database authority differs')
        identity=target_identity(database)
        value['code_sha256']=code_digest();atomic_yaml(path,value);path.chmod(0o400)
        (root/'qualified-code.json').write_text(json.dumps({'previous_code_sha256':args.expected_code_sha256,
            'application_sha256':code_digest(),'database_identity':identity},indent=2)+'\n')
        print('Advanced only the exact synthetic selector code; current PostgreSQL data retained')
        return
    if args.action=='configure-launcher':
        import re,yaml
        if not args.cgroup_parent or not re.fullmatch(r'booksysprfpwqualification[a-z0-9]*\.slice',args.cgroup_parent):
            raise SystemExit('Select a separate isolated qualification slice')
        path=root/'launcher.compose.yaml';value=yaml.safe_load(path.read_text())
        service=value['services']['reader-launcher']
        service['cgroup_parent']=args.cgroup_parent
        command=service['command'];command[command.index('--cgroup-parent')+1]=args.cgroup_parent
        path.write_text(yaml.safe_dump(value))
        print('Configured isolated launcher and child cgroup; no runtime mutation')
        return
    if args.action=='initialize':
        database=Database(private(root/'secrets/migration.dsn'),schema)
        store=Store.create(state,database=database,authority='t5-isolated-reader',state=empty())
        provision(database,application='booksys_app',launcher='booksys_launcher',definer='booksys_launcher_functions',
            app_password=private(root/'secrets/app-password'),launcher_password=private(root/'secrets/launcher-password'))
        for directory,role in ((state,'app'),(root/'launcher-db','launcher')):
            config=directory/'postgresql.json'
            config.write_text(json.dumps({'schema':schema,'dsn_file':str(root/'secrets'/(role+'.dsn'))}))
            config.chmod(0o600)
        atomic_yaml(state/'storage-authority.yaml',{'schema':2,'backend':'postgresql',
            'authority_id':store.authority,'catalogue_path':'postgresql.json','source_digest':'',
            'source_semantic_sha256':'','code_sha256':code_digest(),
            'runtime':{'python_minor':[3,14],'postgresql_min':[18,6]},'compatible_legacy_commit':None})
        (state/'storage-authority.yaml').chmod(0o400)
        catalog=Catalog(state)
        catalog.backend().commit(Change(upserts={'works':{'app-proof':{'title':'Application role write'}}}),expected_revision=0)
        if catalog.get('works','app-proof')!={'title':'Application role write'}:raise RuntimeError('App role public readback failed')
        print('Initialized isolated schema and real roles; app public commit/readback passed')
        return
    os.environ.update(BOOKSYS_READER_SOCKET=str(root/'reader-ipc/reader.sock'),
        BOOKSYS_READER_INPUTS=str(root/'inputs'),BOOKSYS_READER_JOBS=str(root/'jobs'),
        BOOKSYS_READER_REQUESTS=str(state/'reader-requests'))
    catalog=Catalog(state);tasks=StageTasks(catalog.backend());stamp=datetime.now(timezone.utc)
    task=tasks.discover(Obligation('ocr','isolated-reader','input','method'),now=stamp);key=task.key+':1'
    resources=Resources.create(state/'stage-resources.yaml')
    if resources.admit(key,age=stamp,demand=Demand(memory_mib=1792,cpu_millis=1250,disk=1,output_bytes=4*1024**3),free_bytes=100*1024**3,external_bytes=0):raise RuntimeError('Synthetic grant refused')
    owner=DecoderOwnership.create(state/'stage-children'/(key+'.yaml'))
    task=tasks.start(task.key,task.generation,now=stamp,resources=True)
    from PIL import Image
    png=io.BytesIO();Image.new('RGB',(64,64),'white').save(png,format='PNG')
    source=root/'inputs/downloads/synthetic.cbz'
    with zipfile.ZipFile(source,'w') as archive:archive.writestr('001.png',png.getvalue())
    results={};started=time.monotonic()
    with owner.track():
        results['validate']=execute('validate',source)
        results['proof']=execute('proof',source,{'page':1})
        results['ocr']=execute('ocr',source,{'pages':results['validate']['page_manifest']})
        import base64
        results['transform']=execute('transform',source,{'comicinfo':base64.b64encode(b'<ComicInfo><Title>Synthetic</Title></ComicInfo>').decode()})
        bundle=root/'inputs/downloads/bundle.cbz'
        with zipfile.ZipFile(bundle,'w') as archive:archive.writestr('album.cbz',source.read_bytes())
        results['bundle']=execute('bundle',bundle)
    expected={'validate':'validated','ocr':'ok','transform':'validated','bundle':'extracted'}
    if any(results[name].get('status')!=status for name,status in expected.items()) or not results['proof'].get('png'):
        raise RuntimeError('Isolated native reader result failed')
    if not owner.cleanup():raise RuntimeError('Exact stopped worker proof unavailable; retain grant')
    resources.release(key,stopped=True,children_stopped=True)
    tasks.complete(task.key,task.generation,now=datetime.now(timezone.utc),proof='isolated-reader-exact-stop')
    (root/'reader-result.json').write_text(json.dumps({'result':'PASS','candidate':binding['candidate'],
        'application_sha256':code_digest(),'seconds':time.monotonic()-started,
        'operations':{name:result.get('status','png') for name,result in results.items()},
        'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'retained_jobs':sorted(path.name for path in (root/'jobs').iterdir())},indent=2)+'\n')
    print('PASS all five native routes through actual IPC launcher; exact workers stopped, proof retained')


if __name__=='__main__':main()
