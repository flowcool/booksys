"""Explicit inspected retry of one retained lab retirement intent. No blind resend."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from library.catalog import Catalog
from library.operational_state import NativeHistory
from library.primitives import LibraryError, digest, lock, now
from library.retirement import Retirement
from library.stage_execution import effect_scopes, record_scopes
from retirement_history import read_history, package_identity
from ops.retire_packages import PacedPyload

OP=Path('/data/booksys/postgresql-prfpw-20261111')


def native_paths(runtime: dict[str,Any]) -> tuple[Path,Path]:
    roles={r['role']:r for r in runtime['containers']}
    reception=Path(next(m['Source'] for m in roles['retirement']['mounts']
                        if m['Destination']=='/data/booksys/downloads'))
    config=Path(next(m['Source'] for m in roles['pyload']['mounts'] if m['Destination']=='/config'))
    return reception,config/'scripts'


def resume(service: Retirement, source: str, expected: str, restarted: float,
           restart_proof: str) -> dict[str,Any]:
    """The operator command owns inspection; automatic reconciliation stays unchanged."""
    with ExitStack() as held:
        held.enter_context(lock(service.submissions.parent/'pipeline.lock',blocking=False))
        held.enter_context(lock(service.routing.with_suffix('.lock'),blocking=False))
        history=read_history(service.path)
        record=history['acquisitions'].get(source,{})
        if (record.get('phase')!='deleting' or record.get('evidence_digest')!=expected
                or record.get('qualified_retry') or not record.get('packages')
                or not record.get('files') or len(restart_proof)!=64
                or restarted<=datetime.fromisoformat(record['created_at']).timestamp()):
            raise LibraryError('Inspected retirement retry is not qualified')
        if any(s!=source and r['phase']=='deleting' for s,r in history['acquisitions'].items()):
            raise LibraryError('Another removal is uncertain')
        scopes=record_scopes({'origins':record['origins']})+[
            'asset:'+o['sha256'] for o in record['origins'].values() if o.get('sha256')]
        held.enter_context(effect_scopes(service.store.directory,[*scopes,'acquisition:'+digest(source)]))
        mutation=held.enter_context(service.client.mutation('retirement:'+source,service.path))
        if mutation is None:raise LibraryError('Native mutation authority is required')
        pointer=mutation.read()
        if pointer['state']=='uncertain' and (pointer.get('owner')!=mutation.owner
                or pointer.get('journal')!=str(service.path.absolute())):
            raise LibraryError('Another native mutation is uncertain')
        live=service.client.call('get_queue_data')+service.client.call('get_collector_data')
        by_pid={str(p['pid']):p for p in live}
        for pid,original in record['packages'].items():
            current=by_pid.get(pid)
            if (current is None or current['dest']!=0 or package_identity(current)!=package_identity(original)
                    or any(f['status'] not in (0,1,2,3,4,5,6,8,9) for f in current['links'])):
                raise LibraryError('Retirement target identity or stopped state changed')
        for pid,fids in record['unrelated'].items():
            if pid not in by_pid or not set(fids).issubset({f['fid'] for f in by_pid[pid]['links']}):
                raise LibraryError('Unrelated native IDs changed')
        ids=[p['pid'] for p in record['packages'].values()]
        if not service._hooks_safe() or any(d['package_id'] in ids for d in service.client.call('status_downloads')):
            raise LibraryError('Target processing or deletion hooks are not qualified')
        service._verify_files(record)
        proof='inspected-retirement:'+digest([expected,restarted,restart_proof,
            [package_identity(by_pid[pid]) for pid in sorted(record['packages'])]])
        # Persist before the pointer transition or native write. A failed or lost
        # result preserves this one-shot marker and forbids another invocation.
        record['qualified_retry']={'at':now(),'state':'intent','proof':proof,'restart_receipt':restart_proof}
        NativeHistory(service.path.parent).retire(source,record)
        if pointer['state']=='uncertain':mutation.resolve(proof)
        service.client.call('delete_packages',write=True,package_ids=ids)
        remaining=service.client.call('get_queue_data')+service.client.call('get_collector_data')
        after={str(p['pid']):{f['fid'] for f in p['links']} for p in remaining}
        if any(str(pid) in after for pid in ids):raise LibraryError('Removal not confirmed; preserve intent')
        if any(pid not in after or not {f['fid'] for f in p['links']}.issubset(after[pid])
               for pid,p in by_pid.items() if pid not in record['packages']):
            raise LibraryError('Unrelated native IDs disappeared')
        result=service._confirm(history,record)
        mutation.resolve('retirement-readback:'+digest(result))
        return result


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True)
    parser.add_argument('--evidence-digest',required=True)
    parser.add_argument('--restart-receipt',type=Path,required=True)
    parser.add_argument('--apply',action='store_true',required=True)
    args=parser.parse_args()
    runtime=json.loads((OP/'runtime.json').read_text())
    expected=next(r for r in runtime['containers'] if r['role']=='pyload')
    actual=json.loads(subprocess.check_output(['docker','inspect',expected['id']],text=True))[0]
    receipt=json.loads(args.restart_receipt.read_text())
    retained=next(r for r in receipt['containers'] if r['role']=='pyload')
    if ((actual['Id'],actual['Image'],actual['Mounts'])!=(expected['id'],expected['image'],expected['mounts'])
            or retained['id']!=actual['Id'] or retained['state']['StartedAt']!=actual['State']['StartedAt']
            or not actual['State']['Running']):
        raise LibraryError('Exact restarted pyLoad identity differs')
    restarted=datetime.fromisoformat(actual['State']['StartedAt'].replace('Z','+00:00')).timestamp()
    os.environ['BOOKSYS_ADMISSION_STATE']='/data/booksys/library-lab/state'
    client=PacedPyload('http://127.0.0.1:8000',json.loads(Path('/data/booksys/secrets/pyload.json').read_text())['api_key'],1.5)
    reception,scripts=native_paths(runtime)
    service=Retirement(client,Catalog(Path('/data/booksys/library-lab/state')),reception,
        Path('/data/booksys/state/monitor.yaml'),Path('/data/booksys/state/state.yaml'),
        scripts=scripts,concurrent=True)
    result=resume(service,args.source,args.evidence_digest,restarted,hashlib.sha256(args.restart_receipt.read_bytes()).hexdigest())
    print(json.dumps(result))


if __name__=='__main__':main()
