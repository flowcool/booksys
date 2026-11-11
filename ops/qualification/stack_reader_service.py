"""One retained domain grant through the actual launcher and NAS file boundary.

Synthetic files only. No live catalogue, downloader, comics or host mount change.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from library.authority import code_digest
from library.catalog import empty
from library.decoder_ownership import DecoderOwnership
from library.migration import import_catalogue, activate_catalogue
from library.primitives import atomic_yaml
from library.reader_client import execute, available
from library.stage_model import Obligation
from library.stage_resources import Resources, Demand
from library.stage_tasks import StageTasks


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launcher', required=True)
    parser.add_argument('--reader', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    for image in (args.launcher, args.reader):
        if not image.startswith('sha256:') or run('docker', 'run', '--rm', '--network=none',
                '--read-only', '--entrypoint=/app/.venv/bin/python', image, '-c',
                'from library.authority import code_digest; print(code_digest())') != code_digest():
            raise RuntimeError('Use exact images matching the fixture code')
    args.evidence.mkdir(parents=True, mode=0o700, exist_ok=False)
    identity = uuid.uuid4().hex
    nas = Path('/mnt/booksys-host/booksys-files')
    if run('findmnt', '--noheadings', '--output', 'SOURCE,FSTYPE', '--mountpoint', str(nas)).split() != [
            '192.0.2.10:/volume2/storage/booksys', 'nfs']:
        raise RuntimeError('Qualified NAS identity is absent; no scratch writes')
    scratch = nas/'work'/('.qual-compose-'+identity)
    inputs, jobs = scratch/'inputs', scratch/'jobs'
    for path in (inputs/'downloads', jobs):
        path.mkdir(parents=True, mode=0o700)
    state, ipc = args.evidence/'state', args.evidence/'ipc'
    ipc.mkdir(mode=0o700)
    atomic_yaml(state/'library.yaml', empty())
    store = import_catalogue(state, empty(), authority='synthetic-reader-'+identity,
        source_digest=hashlib.sha256((state/'library.yaml').read_bytes()).hexdigest())
    activate_catalogue(state, authority=store.authority)
    StageTasks.upgrade(store)
    tasks = StageTasks(store)
    resources = Resources.create(state/'stage-resources.yaml')
    stamp = datetime.now(timezone.utc)
    task = tasks.discover(Obligation('ocr', 'synthetic-proof', '1', 'qualification-v1'), now=stamp)
    key = task.key+':'+str(task.generation+1)
    if resources.admit(key, age=stamp, demand=Demand(memory_mib=1792, cpu_millis=1250,
            disk=1, output_bytes=4*1024**3), free_bytes=100*1024**3, external_bytes=0):
        raise RuntimeError('Synthetic grant refused')
    owner = DecoderOwnership.create(state/'stage-children'/(key+'.yaml'))
    task = tasks.start(task.key, task.generation, now=stamp, resources=True)
    from PIL import Image
    import io
    data = io.BytesIO()
    Image.new('RGB', (64, 64), 'white').save(data, format='PNG')
    source = inputs/'downloads/synthetic.cbz'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('001.png', data.getvalue())
    contract = {'schema': 1, 'mounts': [
        {'target': '/inputs', 'root': '/',
         'source': '192.0.2.10:/volume2/storage/booksys/'+str(inputs.relative_to(nas)), 'readonly': True},
        {'target': str(jobs), 'root': '/',
         'source': '192.0.2.10:/volume2/storage/booksys/'+str(jobs.relative_to(nas)), 'readonly': False}],
        'sentinels': [], 'probe_directory': str(jobs)}
    config = args.evidence/'nas.json'
    config.write_text(json.dumps(contract))
    name = 'booksys-launcher-probe-'+identity
    container = run('docker', 'create', '--name', name, '--network=none', '--read-only',
        '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=1000:1000',
        '--group-add', str(Path('/var/run/docker.sock').stat().st_gid),
        '--memory=256m', '--memory-swap=256m', '--cpus=.25', '--pids-limit=48',
        '--env', 'BOOKSYS_COMPOSE_PROJECT=booksys-qualification',
        '--env', 'BOOKSYS_NAS_CONTRACT=/nas.json',
        '--mount', f'type=bind,src={config},dst=/nas.json,readonly',
        '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
        '--mount', f'type=bind,src={state},dst=/state',
        '--mount', f'type=bind,src={ipc},dst=/ipc',
        '--mount', f'type=bind,src={inputs},dst=/inputs,readonly',
        '--mount', f'type=bind,src={jobs},dst={jobs}', args.launcher,
        '-m', 'library.reader_service', '--socket', '/ipc/reader.sock', '--inputs', '/inputs',
        '--jobs', str(jobs), '--state', '/state', '--image', args.reader, '--job-uid', '1003',
        '--cgroup-parent', 'system.slice')
    result = {'schema': 1, 'scratch': str(scratch), 'container': container,
              'application_sha256': code_digest(), 'launcher': args.launcher, 'reader': args.reader,
              'limits': 'NAS/service/domain-grant smoke only; shared aggregate cgroup and crash gates remain separate'}
    os.environ.update(BOOKSYS_READER_SOCKET=str(ipc/'reader.sock'), BOOKSYS_READER_INPUTS=str(inputs),
        BOOKSYS_READER_JOBS=str(jobs), BOOKSYS_READER_REQUESTS=str(state/'reader-requests'))
    try:
        run('docker', 'start', container)
        ready = False
        for _ in range(30):
            if available():
                ready = True
                break
            time.sleep(.2)
        if not ready:
            raise RuntimeError('NAS/image readiness unavailable; no job dispatch')
        with owner.track():
            validated = execute('validate', source)
            proof = execute('proof', source, {'page': 1})
        if validated['status'] != 'validated' or not proof.get('png'):
            raise RuntimeError('Synthetic reader route failed')
        if not owner.cleanup():
            raise RuntimeError('Exact worker stop unproved; retain grant')
        resources.release(key, stopped=True, children_stopped=True)
        tasks.finish(task.key, task.generation, now=datetime.now(timezone.utc), outcome='done', proof='synthetic-stopped')
        result.update(result='PASS', validated=validated['status'], proof_png=True, stopped=True,
                      grant_released=resources.reservation(key) is None)
    finally:
        run('docker', 'stop', '--time', '2', container)
        (args.evidence/'launcher-inspect.json').write_text(run('docker', 'inspect', container))
        logs = subprocess.run(['docker', 'logs', container], capture_output=True, text=True)
        (args.evidence/'launcher.log').write_text(logs.stdout+logs.stderr)
        if run('docker', 'inspect', '--format', '{{.State.Running}}', container) != 'false':
            raise RuntimeError('Launcher stop unproved')
        (args.evidence/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print('PASS: NAS input, retained domain grant, service IPC, validation/proof and exact stopped worker reconciliation')


if __name__ == '__main__':
    main()
