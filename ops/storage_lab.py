"""Versioned lab storage conversion. Historical conversion needs operator authority.

Coordinate and quiesce owners first. Drain requests graceful shutdown only. Failed
or partial imports remain retained; writers stay stopped until inspected.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.primitives import LibraryError
from library.recovery import freeze, checkpoint
from library.store import Store
from library.storage_tool import main as storage
from library.migration import create_activity
from activity import STAGES, read_yaml

ROOT = Path('/data/booksys/library-lab')
COLLECTOR = Path('/data/booksys/state')


def run(*args: str) -> str:
    environment = os.environ.copy()
    if args and args[0] == 'systemctl':
        environment.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
        environment.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path=/run/user/{os.getuid()}/bus')
    return subprocess.run(args, env=environment, capture_output=True, text=True, check=True).stdout


def targets() -> dict[str, Any]:
    containers = json.loads(run('docker', 'inspect', 'booksys-worker-1', 'booksys-byparr-1', 'booksys-pyload'))
    return {row['Name'].strip('/'): {'id':row['Id'], 'image':row['Image'],
        'running':row['State']['Running'], 'logging':row['HostConfig']['LogConfig']} for row in containers}


def catalogue_source_digest(source: Path) -> str:
    if source.is_dir() and not source.is_symlink():
        from library.authority import read_manifest
        return str(read_manifest(source.parent)['source_digest'])
    with source.open('rb') as incoming:
        return hashlib.file_digest(incoming, 'sha256').hexdigest()


def activity_inputs(root: Path, collector: Path) -> tuple[dict[str, Any], dict[str, str]]:
    legacy, hashes = {}, {}
    for stage in STAGES:
        locations = [(collector / 'activity', 'collector')] if stage == 'collection' else [
            (collector / 'activity', 'collector'),(root / 'evidence/activity','observer')] if stage == 'byparr' else [
            (root / 'evidence/activity','observer' if stage == 'downloads' else 'controller')]
        for directory, scope in locations:
            path = directory / (stage + '.yaml')
            if not path.exists(): continue
            if path.is_symlink(): raise LibraryError('Unsafe legacy Activity source')
            raw = path.read_bytes(); value = read_yaml(path)
            legacy[stage + ':' + scope] = value
            hashes[str(path)] = hashlib.sha256(raw).hexdigest()
    return legacy, hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inspect','drain','resume','quiesce','convert','activate'))
    parser.add_argument('--authority', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-source')
    parser.add_argument('--expected-worker')
    parser.add_argument('--expected-unit-sha256')
    parser.add_argument('--frozen-receipt', type=Path)
    parser.add_argument('--historical-conversion-authorized', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    source = ROOT / 'state/library.yaml'
    observed = targets()
    from library.authority import code_digest
    if args.frozen_receipt:
        if args.frozen_receipt.is_symlink(): raise LibraryError('Unsafe frozen receipt')
        frozen = json.loads(args.frozen_receipt.read_text())
        if frozen['code_sha256'] != code_digest(): raise LibraryError('Frozen code differs')
        args.expected_source = frozen['source_sha256']
        args.expected_worker = frozen['targets']['booksys-worker-1']['id']
    if args.command == 'inspect':
        digest = catalogue_source_digest(source)
        result = {'schema':1,'code_sha256':code_digest(),'source_sha256':digest,
            'targets':observed,'unit_fragment':run('systemctl','--user','show','booksys-library.service',
                '--property=FragmentPath','--property=ActiveState').splitlines()}
        args.evidence.parent.mkdir(parents=True,exist_ok=True)
        args.evidence.write_text(json.dumps(result,indent=2)+'\n'); return
    if args.command in ('drain', 'resume'):
        if observed['booksys-worker-1']['id'] != args.expected_worker:
            raise LibraryError('Collector target drift')
        unit = Path.home()/'.config/systemd/user/booksys-library.service'
        previous = unit.read_bytes()
        if hashlib.sha256(previous).hexdigest() != args.expected_unit_sha256:
            raise LibraryError('Library unit drift')
        policy = run('systemctl','--user','show','booksys-library.service',
                     '--property=KillMode','--property=TimeoutStopUSec').splitlines()
        if set(policy) != {'KillMode=process', 'TimeoutStopUSec=infinity'}:
            raise LibraryError('Graceful drain policy is not verified')
        args.evidence.mkdir(mode=0o700)
        (args.evidence/'previous.service').write_bytes(previous)
        action = 'stop' if args.command == 'drain' else 'start'
        run('systemctl','--user',action,'--no-block','booksys-library.service')
        (args.evidence/'receipt.json').write_text(json.dumps({
            'status':'graceful_' + action + '_requested', 'targets':observed, 'stop_policy':policy},indent=2)+'\n')
        return
    if args.command == 'quiesce':
        if observed['booksys-worker-1']['id'] != args.expected_worker:
            raise LibraryError('Collector target drift')
        # The retirement owner must first release its driver. Nonblocking locks
        # refuse every active effect; never terminate an in-flight publication.
        with freeze(Store(ROOT/'state',authority=args.authority),COLLECTOR):
            args.evidence.mkdir(mode=0o700)
            unit = Path.home()/'.config/systemd/user/booksys-library.service'
            previous = unit.read_bytes()
            (args.evidence/'previous.service').write_bytes(previous)
            run('systemctl','--user','stop','booksys-library.service')
            if observed['booksys-worker-1']['running']:
                run('docker','stop','--timeout','30',args.expected_worker)
            stopped = targets()
            if stopped['booksys-worker-1']['running'] or any(stopped[k]['id']!=v['id'] for k,v in observed.items()):
                raise LibraryError('Quiesced target drift; keep writers stopped')
            digest = catalogue_source_digest(source)
            result={'schema':1,'code_sha256':code_digest(),'source_sha256':digest,
                'targets':stopped,'unit_sha256':hashlib.sha256(previous).hexdigest()}
            (args.evidence/'frozen.json').write_text(json.dumps(result,indent=2)+'\n')
        return
    if not args.historical_conversion_authorized:
        raise LibraryError('Historical conversion authorization is required')
    if observed['booksys-worker-1']['id'] != args.expected_worker or observed['booksys-worker-1']['running']:
        raise LibraryError('Expected stopped collector target required')
    state = run('systemctl','--user','show','booksys-library.service','--property=ActiveState','--value').strip()
    if state not in ('inactive','failed'):
        raise LibraryError('Library service must be quiesced')
    args.evidence.mkdir(mode=0o700)
    store = Store(ROOT / 'state',authority=args.authority)
    if args.command == 'convert':
        if not args.expected_source:
            raise LibraryError('Frozen source digest is required')
        with source.open('rb') as incoming: digest=hashlib.file_digest(incoming,'sha256').hexdigest()
        if digest != args.expected_source:
            raise LibraryError('Frozen source differs; keep writers stopped')
        code = storage(['--state',str(ROOT/'state'),'--authority',args.authority,
                        '--collector',str(COLLECTOR),'--expected-source',args.expected_source,'import'])
        if code: raise LibraryError('Catalogue import failed; keep writers stopped')
        with freeze(store,COLLECTOR):
            legacy, hashes = activity_inputs(ROOT,COLLECTOR)
            activity = ROOT/'state/activity'
            if activity.exists(): raise LibraryError('Activity target already exists; inspect retained import')
            create_activity(activity,authority=args.authority+'-activity',timestamp=time.time(),legacy=legacy)
            _, after = activity_inputs(ROOT,COLLECTOR)
            if hashes != after: raise LibraryError('Activity source drift; keep writers stopped')
            (args.evidence/'activity-source-hashes.json').write_text(json.dumps(hashes,indent=2)+'\n')
        checkpoint(store,args.evidence/'verified-point',collector=COLLECTOR,activity=activity)
    else:
        code = storage(['--state',str(ROOT/'state'),'--authority',args.authority,
                        '--collector',str(COLLECTOR),'activate'])
        if code: raise LibraryError('Authority selection failed; keep writers stopped')
    (args.evidence/'receipt.json').write_text(json.dumps({'schema':1,'status':args.command,
        'authority':args.authority,'revision':store.revision,'code_sha256':code_digest()},indent=2)+'\n')


if __name__ == '__main__': main()
