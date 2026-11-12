"""Exact-candidate BETA/LAB cutover and current-state serial recovery.

Never import historical data, remove a stack container or restore an old store.
Run prepare before apply. A failed write stops the operation with evidence kept.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import yaml

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from library.authority import code_digest, read_manifest
from library.primitives import LibraryError, atomic_yaml, lock
from library.stage_model import QUEUES
from library.stage_supervisor import units

ROOT = Path('/data/booksys/library-lab')
COLLECTOR = Path('/data/booksys/state')
RECEPTION = Path('/data/booksys/downloads')
CREDENTIALS = Path('/data/booksys/secrets/pyload.json')
USER_UNIT = Path.home()/'.config/systemd/user/booksys-library.service'
SYSTEM = Path('/etc/systemd/system')


def command(*argv: str, timeout: int = 60) -> str:
    raise LibraryError('Historical activation tooling is retired')
    environment = os.environ.copy()
    environment.update(XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}',
                       DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
    result = subprocess.run(argv, capture_output=True, text=True, env=environment, timeout=timeout)
    if result.returncode:
        raise LibraryError('Command failed: '+argv[0]+'; '+result.stderr[:512])
    return result.stdout.strip()


def names(mode: str) -> list[str]:
    return ['booksys-stage-'+queue+'.service' for queue in (QUEUES if mode == 'independent' else ('serial',))]


def render(image: str) -> dict[str, str]:
    result = units(code=CODE, python=Path(sys.executable), root=ROOT, collector=COLLECTOR,
                   reception=RECEPTION, credentials=CREDENTIALS, reader_image=image)
    for name in result:
        if name.endswith('.service'):
            result[name] = result[name].replace('--queue ', '--byparr http://127.0.0.1:8191/v1 --queue ')
            result[name] += '\n'
    ip = command('docker', 'inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', 'booksys-byparr-1')
    import ipaddress
    if not ipaddress.ip_address(ip).is_private:
        raise LibraryError('Unexpected local Byparr target')
    result['booksys-byparr-proxy.socket'] = ('[Unit]\nDescription=Loopback access to the existing Byparr\n'
        '[Socket]\nListenStream=127.0.0.1:8191\nNoDelay=true\n[Install]\nWantedBy=sockets.target\n')
    result['booksys-byparr-proxy.service'] = ('[Unit]\nDescription=Existing Byparr socket proxy\n'
        '[Service]\nUser='+str(os.getuid())+'\nGroup='+str(os.getgid())+'\n'
        'ExecStart=/usr/lib/systemd/systemd-socket-proxyd '+ip+':8191\n'
        'Slice=booksys.slice\nMemoryMax=32M\nNoNewPrivileges=true\n')
    result['booksys-stage-observer.service'] = ('[Unit]\nDescription=Booksys HTTP and shared observation\n'
        '[Service]\nType=simple\nUser='+str(os.getuid())+'\nGroup='+str(os.getgid())+'\nUMask=0077\n'
        'WorkingDirectory='+str(CODE)+'\nExecStart='+str(Path(sys.executable))+' '+str(CODE/'ops/library_service.py')+
        ' --root '+str(ROOT)+' --credentials '+str(CREDENTIALS)+' --listen 192.0.2.10 --port 8768 --activity --independent-queues\n'
        'Slice=booksys.slice\nMemoryMax=1024M\nMemorySwapMax=0\nCPUQuota=50%\nKillMode=control-group\n'
        'TimeoutStopSec=30\nRestart=on-failure\nRestartSec=5\n'
        'Environment=TMPDIR=/data/tmp\nEnvironment=XDG_CACHE_HOME=/data/cache/flow\n'
        'Environment=BOOKSYS_STAGE_MODE=1\nEnvironment=BOOKSYS_ADMISSION_STATE='+str(ROOT/'state')+'\n'
        'Environment=BOOKSYS_ACTIVITY_DIR='+str(ROOT/'state/activity')+'\n'
        'Environment=BOOKSYS_READER_IMAGE='+image+'\n[Install]\nWantedBy=multi-user.target\n')
    return result


def prepare(evidence: Path, image: str, qualification: Path) -> None:
    if command('hostname') != 'booksys-host' or command('docker', 'info', '--format', '{{.Name}}') != 'booksys-host':
        raise LibraryError('Unexpected lab control plane')
    if command('git', '-C', str(CODE), 'status', '--porcelain'):
        raise LibraryError('Commit the complete candidate first')
    expected = code_digest()
    receipt = yaml.safe_load(qualification.read_text())
    if receipt.get('application_sha256') != expected or receipt.get('result') != 'PASS':
        raise LibraryError('Qualification does not bind this application')
    image_id = command('docker', 'image', 'inspect', '--format', '{{.Id}}', image)
    image_digest = command('docker', 'run', '--rm', '--network=none', '--entrypoint=/app/.venv/bin/python', image_id,
                           '-c', 'from library.authority import code_digest; print(code_digest())')
    if image_digest != expected:
        raise LibraryError('Reader image differs from committed application')
    command('sudo', '-n', 'test', '-w', str(SYSTEM))
    from booksys_core.downloader import Pyload
    client = Pyload('http://127.0.0.1:8000', json.loads(CREDENTIALS.read_text())['api_key'])
    configuration = {key: client.call('get_config_value', category='download', option=key)
                     for key in ('limit_speed', 'max_speed', 'max_downloads', 'chunks')}
    # the operator retains unlimited shared pyLoad (2026-11-09). Unknown demand
    # remains explicit; resource admission defers new disk-consuming work.
    bound = {'configuration': configuration, 'policy': 'unbounded-defer-disk',
             'overshoot_bytes': None}
    previous = USER_UNIT.read_bytes()
    authority = yaml.safe_load((ROOT/'state/storage-authority.yaml').read_text())
    read_manifest(ROOT/'state', expected_code=authority['code_sha256'])
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    (evidence/'previous.service').write_bytes(previous)
    (evidence/'previous-authority.yaml').write_bytes((ROOT/'state/storage-authority.yaml').read_bytes())
    unitdir = evidence/'units'
    unitdir.mkdir(mode=0o700)
    rendered = render(image_id)
    for name, text in rendered.items():
        (unitdir/name).write_text(text)
    command('systemd-analyze', 'verify', *[str(unitdir/name) for name in rendered])
    identities = json.loads(command('docker', 'inspect', '--format', '{{json .}}', 'booksys-worker-1'))
    for name in rendered:
        if (SYSTEM/name).exists():
            raise LibraryError('Conflicting system unit')
    record = {'schema': 1, 'code_sha256': expected, 'commit': command('git', '-C', str(CODE), 'rev-parse', 'HEAD'),
        'authority_sha256': hashlib.sha256((ROOT/'state/storage-authority.yaml').read_bytes()).hexdigest(),
        'unit_sha256': hashlib.sha256(previous).hexdigest(), 'collector_id': identities['Id'],
        'collector_running': identities['State']['Running'], 'reader_image': image_id,
        'qualification': str(qualification.absolute()), 'pyload_bound': bound,
        'units': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in rendered.items()}}
    atomic_yaml(evidence/'plan.yaml', record)


def verify_downloader(plan: dict[str, Any]) -> None:
    from booksys_core.downloader import Pyload
    client = Pyload('http://127.0.0.1:8000', json.loads(CREDENTIALS.read_text())['api_key'])
    actual = {key: client.call('get_config_value', category='download', option=key)
              for key in ('limit_speed', 'max_speed', 'max_downloads', 'chunks')}
    if actual != plan['pyload_bound']['configuration']:
        raise LibraryError('Shared downloader configuration drift')


def apply(evidence: Path) -> None:
    plan = yaml.safe_load((evidence/'plan.yaml').read_text())
    if (plan['commit'] != command('git', '-C', str(CODE), 'rev-parse', 'HEAD')
            or plan['authority_sha256'] != hashlib.sha256((ROOT/'state/storage-authority.yaml').read_bytes()).hexdigest()
            or plan['code_sha256'] != code_digest() or command('git', '-C', str(CODE), 'status', '--porcelain')
            or hashlib.sha256(USER_UNIT.read_bytes()).hexdigest() != plan['unit_sha256']):
        raise LibraryError('Candidate or old service drift')
    if command('docker', 'inspect', '--format', '{{.Id}}', 'booksys-worker-1') != plan['collector_id']:
        raise LibraryError('Collector identity drift')
    for name, expected in plan['units'].items():
        if hashlib.sha256((evidence/'units'/name).read_bytes()).hexdigest() != expected or (SYSTEM/name).exists():
            raise LibraryError('Unit artifact drift or conflicting system owner')
    verify_downloader(plan)
    # All writes from here are part of the recorded operation. Failures stop;
    # inspect retained phase/readback before deliberate recovery.
    atomic_yaml(evidence/'phase.yaml', {'phase': 'stopping-legacy'})
    command('systemctl', '--user', 'stop', 'booksys-library.service', timeout=600)
    with ExitStack() as guards:
        guards.enter_context(lock(ROOT/'state/library-cycle.lock', blocking=False))
        guards.enter_context(lock(COLLECTOR/'pipeline.lock', blocking=False))
        if plan['authority_sha256'] != hashlib.sha256((ROOT/'state/storage-authority.yaml').read_bytes()).hexdigest():
            raise LibraryError('Selected authority drift under writer locks')
        verify_downloader(plan)
        command('docker', 'stop', '--time', '120', 'booksys-worker-1', timeout=150)
        if command('docker', 'inspect', '--format', '{{.State.Running}}', 'booksys-worker-1') != 'false':
            raise LibraryError('Legacy collector is still running')
        atomic_yaml(evidence/'phase.yaml', {'phase': 'legacy-quiesced', 'collector_id': plan['collector_id']})
        from library.store import Store
        from library.migration import manifest
        from library.stage_tasks import StageTasks
        from library.source_tasks import SourcePolicy
        from library.stage_resources import Resources, Demand
        from booksys_core.network_admission import RollingBudget
        old = yaml.safe_load((evidence/'previous-authority.yaml').read_text())
        store = Store(ROOT/'state', authority=old['authority_id'])
        store.audit()
        StageTasks.upgrade(store)
        SourcePolicy.create(ROOT/'state/task-policy.yaml')
        Resources.create(ROOT/'state/stage-resources.yaml',
                         capacity=Demand(memory_mib=2048, cpu_millis=2000, disk=1, browser=1, native=1),
                         external_policy='unreserved', reserve_bytes=0)
        for kind, requests, period, spacing in (('provider', 50, 300, 1), ('native', 30, 60, 0)):
            path = ROOT/'state'/f'{kind}-admission.yaml'
            if not path.exists():
                budget = RollingBudget.create(path, requests=requests, period=period, spacing=spacing)
                # The legacy cycle had no durable rolling starts. Conservatively
                # retain its entire possible window before allowing fresh calls.
                budget.defer(time.time()+period)
            else:
                RollingBudget(path).next_due()
        manifest(ROOT/'state', old['authority_id'], 'sqlite')
        atomic_yaml(ROOT/'state/stage-activation.yaml', {'schema': 1, 'code_digest': code_digest(),
            'mode': 'independent', 'supervisor': 'systemd', 'qualification_receipt': plan['qualification'],
            'cgroup_pool': '/sys/fs/cgroup/booksys.slice', 'decoder_cgroup_parent': 'booksys.slice',
            'io_device': f'{os.major(ROOT.stat().st_dev)}:{os.minor(ROOT.stat().st_dev)}', 'pyload_bound': plan['pyload_bound']})
        atomic_yaml(evidence/'phase.yaml', {'phase': 'authority-selected', 'revision': store.revision})
        for name in plan['units']:
            command('sudo', '-n', 'install', '-m', '644', str(evidence/'units'/name), str(SYSTEM/name))
        command('sudo', '-n', 'systemctl', 'daemon-reload')
        command('sudo', '-n', 'systemctl', 'set-property', '--runtime', 'booksys.slice',
                'MemoryMax=3584M', 'MemorySwapMax=0', 'CPUQuota=200%')
    command('systemctl', '--user', 'disable', 'booksys-library.service')
    command('sudo', '-n', 'systemctl', 'enable', '--now', 'booksys-byparr-proxy.socket',
            'booksys-stage-discovery.service', 'booksys-stage-observer.service', *names('independent'), timeout=120)
    atomic_yaml(evidence/'phase.yaml', {'phase': 'independent-started', 'code_sha256': code_digest()})
    status(evidence)


def interrupt_legacy(evidence: Path) -> None:
    """Operator-requested bounded interruption; preserve current journals."""
    phase = yaml.safe_load((evidence/'phase.yaml').read_text())
    plan = yaml.safe_load((evidence/'plan.yaml').read_text())
    if phase.get('phase') != 'stopping-legacy' or plan['code_sha256'] != code_digest():
        raise LibraryError('Legacy interruption target drift')
    if hashlib.sha256(USER_UNIT.read_bytes()).hexdigest() != plan['unit_sha256']:
        raise LibraryError('Legacy unit changed')
    before = command('systemctl', '--user', 'show', 'booksys-library.service',
                     '--property=MainPID', '--property=ControlGroup', '--property=ActiveState')
    if 'ActiveState=deactivating' not in before:
        raise LibraryError('Legacy service is not already stopping')
    children = command('docker', 'ps', '--format', '{{.Names}}', '--filter', 'name=booksys-')
    unexpected = [name for name in children.splitlines() if name not in ('booksys-worker-1', 'booksys-byparr-1')]
    if unexpected:
        raise LibraryError('External decoder remains; retain ownership proof before interruption')
    atomic_yaml(evidence/'legacy-interruption.yaml', {'before': before, 'children': children.splitlines(),
        'authority': 'the operator requests bounded wait on 2026-11-09; stop old cycle and reconcile retained journals',
        'operation_commit': command('git', '-C', str(CODE), 'rev-parse', 'HEAD')})
    command('systemctl', '--user', 'kill', '--kill-whom=all', '--signal=SIGTERM', 'booksys-library.service')
    for _ in range(40):
        if command('systemctl', '--user', 'show', 'booksys-library.service', '--property=MainPID', '--value') == '0':
            return
        time.sleep(.5)
    raise LibraryError('Legacy process exclusion not proved after bounded interruption')


def switch(evidence: Path, mode: str) -> None:
    activation_path = ROOT/'state/stage-activation.yaml'
    activation = yaml.safe_load(activation_path.read_text())
    if activation['code_digest'] != code_digest():
        raise LibraryError('Selected candidate drift')
    command('sudo', '-n', 'systemctl', 'stop', *names(activation['mode']), timeout=60)
    for name in names(activation['mode']):
        if command('systemctl', 'show', name, '--property=MainPID', '--value') != '0':
            raise LibraryError('Prior actor remains alive')
    activation['mode'] = mode
    atomic_yaml(activation_path, activation)
    command('sudo', '-n', 'systemctl', 'disable', *names('serial' if mode == 'independent' else 'independent'))
    command('sudo', '-n', 'systemctl', 'enable', '--now', *names(mode), timeout=120)
    status(evidence)


def pause(evidence: Path) -> None:
    """Stop candidate processes before correcting a failed offline prerequisite."""
    targets = [*names('independent'), *names('serial'),
               'booksys-stage-discovery.service', 'booksys-stage-observer.service']
    command('sudo', '-n', 'systemctl', 'stop', *targets, timeout=60)
    rows = {name: command('systemctl', 'show', name, '--property=MainPID', '--value') for name in targets}
    atomic_yaml(evidence/'paused.yaml', {'code_sha256': code_digest(), 'pids': rows})
    if any(value != '0' for value in rows.values()):
        raise LibraryError('Candidate process remains alive')


def requalify_native_memory(resource: dict[str, Any], tasks: Any) -> dict[str, Any]:
    """Paused replacement only: retain identities/ages and raise native reserves.

    Refuse an overcommitted replacement before any durable change. Existing
    grants are reservations, not proof that an interrupted effect is resolved.
    """
    from copy import deepcopy
    from dataclasses import asdict, replace
    from library.stage_runtime import demand
    result = deepcopy(resource)
    for table in ('grants', 'waiters'):
        for key, record in result[table].items():
            task = tasks.get(key.rsplit(':', 1)[0])
            if task is None or task.stage not in ('collection', 'routing', 'retirement'):
                continue
            expected = demand(task)
            old_memory = 128 if task.stage == 'routing' else 256
            if record['demand'] == asdict(expected):
                continue
            if record['demand'] != asdict(replace(expected, memory_mib=old_memory)):
                raise LibraryError('Unexpected retained native demand')
            record['demand'] = asdict(expected)
    if sum(row['demand']['memory_mib'] for row in result['grants'].values()) > 2048:
        raise LibraryError('Current grants exceed replacement task pool')
    return result


def refresh(evidence: Path, image: str, qualification: Path) -> None:
    """Replace a paused lab candidate without restoring or converting its store."""
    paused = yaml.safe_load((evidence/'paused.yaml').read_text())
    activation = yaml.safe_load((ROOT/'state/stage-activation.yaml').read_text())
    if paused['code_sha256'] != activation['code_digest']:
        raise LibraryError('Paused candidate differs from selected authority')
    for name in paused['pids']:
        if command('systemctl', 'show', name, '--property=MainPID', '--value') != '0':
            raise LibraryError('Candidate must remain paused')
    if command('git', '-C', str(CODE), 'status', '--porcelain'):
        raise LibraryError('Commit the replacement candidate first')
    receipt = yaml.safe_load(qualification.read_text())
    if receipt.get('result') != 'PASS' or receipt.get('application_sha256') != code_digest():
        raise LibraryError('Replacement qualification does not bind this application')
    image_id = command('docker', 'image', 'inspect', '--format', '{{.Id}}', image)
    if receipt.get('reader_image') != image_id or command('docker', 'run', '--rm', '--network=none',
            '--entrypoint=/app/.venv/bin/python', image_id, '-c',
            'from library.authority import code_digest; print(code_digest())') != code_digest():
        raise LibraryError('Replacement reader does not bind this application')
    authority = read_manifest(ROOT/'state', expected_code=activation['code_digest'])
    from library.store import Store
    from library.migration import manifest
    store = Store(ROOT/'state', authority=authority['authority_id'])
    store.audit()
    rendered = render(image_id)
    unit_root = evidence/'replacement-units'
    unit_root.mkdir(exist_ok=False)
    for name, content in rendered.items():
        (unit_root/name).write_text(content)
    command('systemd-analyze', 'verify', *(str(unit_root/name) for name in rendered))
    atomic_yaml(evidence/'replacement-before.yaml', {'activation': activation, 'authority': authority,
                                                   'revision': store.revision})
    # Reserve 1024MiB observer plus 512MiB discovery inside the measured
    # 3584MiB parent. Retain every grant and waiter; never re-create.
    from library.stage_resources import Resources
    from library.stage_tasks import StageTasks
    from library.source_tasks import SourcePolicy
    resource_path = ROOT/'state/stage-resources.yaml'
    with lock(resource_path.with_suffix('.lock')):
        resource = Resources(resource_path)._read()
        if sum(row['demand']['memory_mib'] for row in resource['grants'].values()) > 2048:
            raise LibraryError('Current grants exceed replacement task pool')
        atomic_yaml(evidence/'resources-before.yaml', resource)
        resource = requalify_native_memory(resource, StageTasks(store, policy=SourcePolicy(ROOT/'state/task-policy.yaml')))
        resource['capacity']['memory_mib'] = 2048
        atomic_yaml(resource_path, resource)
    manifest(ROOT/'state', authority['authority_id'], 'sqlite')
    activation.update(code_digest=code_digest(), mode='independent', qualification_receipt=str(qualification))
    atomic_yaml(ROOT/'state/stage-activation.yaml', activation)
    for name in rendered:
        command('sudo', '-n', 'install', '-m', '644', str(unit_root/name), str(SYSTEM/name))
    command('sudo', '-n', 'systemctl', 'daemon-reload')
    command('sudo', '-n', 'systemctl', 'set-property', '--runtime', 'booksys.slice',
            'MemoryMax=3584M', 'MemorySwapMax=0', 'CPUQuota=200%')
    for name in paused['pids']:
        if command('systemctl', 'show', name, '--property=ActiveState', '--value') == 'failed':
            command('sudo', '-n', 'systemctl', 'reset-failed', name)
    command('sudo', '-n', 'systemctl', 'disable', *names('serial'))
    command('sudo', '-n', 'systemctl', 'enable', '--now', 'booksys-stage-discovery.service',
            'booksys-stage-observer.service', *names('independent'), timeout=120)
    atomic_yaml(evidence/'replacement-started.yaml', {'code_sha256': code_digest(), 'image': image_id,
                                                    'revision_before': store.revision})
    status(evidence)


def start_selected(evidence: Path) -> None:
    """Resume the already selected replacement after an interrupted unit start."""
    selected = read_manifest(ROOT/'state')
    activation = yaml.safe_load((ROOT/'state/stage-activation.yaml').read_text())
    receipt = yaml.safe_load(Path(activation['qualification_receipt']).read_text())
    if (activation['code_digest'] != code_digest() or receipt.get('result') != 'PASS'
            or receipt.get('application_sha256') != code_digest()):
        raise LibraryError('Selected replacement is not qualified')
    for name, content in render(receipt['reader_image']).items():
        if (SYSTEM/name).read_text() != content:
            raise LibraryError('Installed unit differs from qualified replacement')
    atomic_yaml(evidence/'resume-selected.yaml', {'code_sha256': code_digest(),
        'authority': selected['authority_id'], 'mode': activation['mode']})
    targets = ['booksys-stage-discovery.service', 'booksys-stage-observer.service', *names(activation['mode'])]
    for name in targets:
        if command('systemctl', 'show', name, '--property=ActiveState', '--value') == 'failed':
            command('sudo', '-n', 'systemctl', 'reset-failed', name)
    command('sudo', '-n', 'systemctl', 'disable', *names('serial' if activation['mode'] == 'independent' else 'independent'))
    command('sudo', '-n', 'systemctl', 'enable', '--now', *targets, timeout=120)
    status(evidence)


def retain_probe_files(evidence: Path) -> None:
    """Retain two unused empty files created by a malformed offline-copy URI."""
    for name in (*names('independent'), *names('serial'),
                 'booksys-stage-discovery.service', 'booksys-stage-observer.service'):
        if command('systemctl', 'show', name, '--property=MainPID', '--value') != '0':
            raise LibraryError('Candidate must remain paused for cleanup')
    paths = [ROOT/'state/catalogue/catalogue.sqlite?mode=ro', ROOT/'state/activity/activity.sqlite?mode=ro']
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != 0 or path.stat().st_uid != os.getuid():
            raise LibraryError('Unexpected probe artifact; preserve and inspect')
    target = evidence/'unused-empty-probe-files'
    target.mkdir(exist_ok=False)
    for path in paths:
        path.rename(target/(path.parent.name+'-unused.sqlite'))
    atomic_yaml(evidence/'probe-cleanup.yaml', {'retained': [str(path) for path in paths],
        'bytes': 0, 'canonical_databases': 'untouched; original URI omitted file: and opened only unused names'})


def observer_cpu(evidence: Path) -> None:
    """Apply the measured HTTP CPU correction without restarting task owners."""
    read_manifest(ROOT/'state')
    activation = yaml.safe_load((ROOT/'state/stage-activation.yaml').read_text())
    receipt = yaml.safe_load(Path(activation['qualification_receipt']).read_text())
    if receipt.get('result') != 'PASS' or receipt.get('application_sha256') != code_digest():
        raise LibraryError('Observer correction requires the qualified candidate')
    name = 'booksys-stage-observer.service'
    content = render(receipt['reader_image'])[name]
    previous = (SYSTEM/name).read_text()
    if previous not in (content, content.replace('CPUQuota=50%', 'CPUQuota=25%')):
        raise LibraryError('Observer unit drift')
    (evidence/'observer-before-cpu.service').write_text(previous)
    target = evidence/name
    target.write_text(content)
    command('systemd-analyze', 'verify', str(target))
    command('sudo', '-n', 'install', '-m', '644', str(target), str(SYSTEM/name))
    command('sudo', '-n', 'systemctl', 'daemon-reload')
    command('sudo', '-n', 'systemctl', 'set-property', '--runtime', name, 'CPUQuota=50%')
    atomic_yaml(evidence/'observer-cpu-readback.yaml', {'code_sha256': code_digest(),
        'unit': command('systemctl', 'show', name, '--property=MainPID', '--property=CPUQuotaPerSecUSec'),
        'pool_cpu_max': Path('/sys/fs/cgroup/booksys.slice/cpu.max').read_text().strip()})
    status(evidence)


def status(evidence: Path) -> None:
    activation = yaml.safe_load((ROOT/'state/stage-activation.yaml').read_text())
    units_now = ['booksys-stage-discovery.service', 'booksys-stage-observer.service', *names(activation['mode'])]
    rows = {name: command('systemctl', 'show', name, '--property=ActiveState', '--property=SubState',
                          '--property=MainPID', '--property=ControlGroup', '--property=MemoryPeak') for name in units_now}
    if any('ActiveState=active' not in value for value in rows.values()):
        raise LibraryError('An expected unit is not active; inspect journals')
    atomic_yaml(evidence/('readback-'+activation['mode']+'.yaml'), {'code_sha256': code_digest(), 'units': rows})


def main() -> None:
    raise SystemExit('Historical activation tooling is retired; use the versioned PostgreSQL cutover operation')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'status', 'serial', 'independent', 'interrupt-legacy', 'pause', 'refresh', 'start-selected', 'retain-probe-files', 'observer-cpu'))
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--image')
    parser.add_argument('--qualification', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action in ('prepare', 'refresh'):
        if not args.image or not args.qualification:
            parser.error('prepare requires image and qualification')
        (prepare if args.action == 'prepare' else refresh)(args.evidence, args.image, args.qualification)
    elif args.action == 'interrupt-legacy':
        interrupt_legacy(args.evidence)
    elif args.action == 'apply':
        apply(args.evidence)
    elif args.action == 'status':
        status(args.evidence)
    elif args.action == 'pause':
        pause(args.evidence)
    elif args.action == 'start-selected':
        start_selected(args.evidence)
    elif args.action == 'retain-probe-files':
        retain_probe_files(args.evidence)
    elif args.action == 'observer-cpu':
        observer_cpu(args.evidence)
    else:
        switch(args.evidence, args.action)


if __name__ == '__main__':
    main()
