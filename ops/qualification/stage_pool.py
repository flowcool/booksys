"""Qualify a dedicated systemd pool shared by a flow actor and Docker decoder."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--candidate-tests', action='store_true')
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    prefix = 'booksysq' + uuid.uuid4().hex[:12]
    unit, pool, container = prefix+'.service', prefix+'.slice', prefix+'-decoder'
    commands = []
    def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=240)
        commands.append({'argv': argv, 'returncode': result.returncode,
                         'stdout': result.stdout[:8192], 'stderr': result.stderr[:8192]})
        if check and result.returncode:
            raise RuntimeError('Qualification command failed; inspect commands.json')
        return result
    try:
        run(['sudo', '-n', 'systemd-run', '--quiet', '--unit='+unit, '--slice='+pool,
            '--uid='+str(os.getuid()), '--property=Type=exec', '--property=KillMode=control-group',
            '--property=MemoryMax=256M', '--property=RuntimeMaxSec=90', '/usr/bin/sleep', '80'])
        run(['sudo', '-n', 'systemctl', 'set-property', '--runtime', pool,
             'MemoryMax=3584M', 'MemorySwapMax=0', 'CPUQuota=200%'])
        run(['docker', 'run', '--rm', '-d', '--name', container, '--cgroup-parent='+pool,
            '--network=none', '--memory=1536m', '--cpus=1', '--pids-limit=32',
            '--entrypoint=/app/.venv/bin/python', args.image, '-c',
            'import time; data=bytearray(64*1024**2); time.sleep(60)'])
        actor_pid = int(run(['systemctl', 'show', unit, '--property=MainPID', '--value']).stdout)
        decoder_pid = int(run(['docker', 'inspect', '--format', '{{.State.Pid}}', container]).stdout)
        groups = {label: Path(f'/proc/{pid}/cgroup').read_text().strip().split('0::')[-1]
                  for label, pid in [('actor', actor_pid), ('decoder', decoder_pid)]}
        expected = '/'+pool+'/'
        if not all(group.startswith(expected) for group in groups.values()):
            raise RuntimeError('Actor and decoder are not in the dedicated shared pool')
        control = Path('/sys/fs/cgroup')/pool
        time.sleep(1)
        result: dict[str, Any] = {'groups': groups, 'pool': pool,
                  'counters': {name: (control/name).read_text() for name in
                      ('memory.current', 'memory.peak', 'memory.max', 'cpu.max', 'io.stat')}}
        if int(result['counters']['memory.max']) != 3584*1024**2:
            raise RuntimeError('Shared memory envelope differs')
        run(['sudo', '-n', 'systemctl', 'stop', unit])
        result['actor_stopped'] = not Path(f'/proc/{actor_pid}').exists()
        result['decoder_survives_actor_stop'] = Path(f'/proc/{decoder_pid}').exists()
        run(['docker', 'stop', '--time', '1', container])
        result['decoder_stopped'] = not Path(f'/proc/{decoder_pid}').exists()
        if not result['actor_stopped'] or not result['decoder_stopped']:
            raise RuntimeError('Owned cleanup is not proved')
        if args.candidate_tests:
            tests = run(['sudo', '-n', 'systemd-run', '--quiet', '--wait', '--pipe', '--unit='+prefix+'tests.service',
                '--slice='+pool, '--uid='+str(os.getuid()), '--property=Type=exec', '--property=KillMode=control-group',
                '--property=MemoryMax=256M', '--property=MemorySwapMax=0', '--property=RuntimeMaxSec=220',
                '--working-directory='+str(Path(__file__).resolve().parents[2]),
                '--setenv=BOOKSYS_READER_IMAGE='+args.image, '--setenv=BOOKSYS_DECODER_CGROUP_PARENT='+pool,
                '--setenv=TMPDIR=/data/tmp', sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                '-p', 'test_stage_runtime.py', '-v'])
            (args.evidence/'candidate-tests.log').write_text(tests.stdout+tests.stderr)
            result['candidate_tests'] = tests.returncode
            # The holder keeps cgroup counters alive through readback.
            result['candidate_counters'] = {name: (control/name).read_text() for name in
                ('memory.peak', 'memory.max', 'cpu.max', 'io.stat') if (control/name).exists()}
        (args.evidence/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    finally:
        run(['sudo', '-n', 'systemctl', 'stop', unit], check=False)
        run(['sudo', '-n', 'systemctl', 'stop', prefix+'tests.service'], check=False)
        run(['docker', 'stop', '--time', '1', container], check=False)
        run(['sudo', '-n', 'systemctl', 'stop', pool], check=False)
        (args.evidence/'commands.json').write_text(json.dumps(commands, indent=2)+'\n')


if __name__ == '__main__':
    main()
