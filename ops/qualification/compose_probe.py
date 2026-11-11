"""Retain identity, bounded output and stop proof for trusted Docker probes.

Host qualification tooling only. This is not an application or Docker API proxy.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import uuid
from typing import Any

from library.process import bounded_run


def owned_probe(command: list[str], evidence: Path, *, timeout: int) -> dict[str, Any]:
    if command[:2] != ['docker', 'create'] or '--name' in command or '--rm' in command:
        raise ValueError('Expected a trusted unnamed non-removing create command')
    name = 'booksys-probe-'+uuid.uuid4().hex
    command = [*command[:2], '--name', name, *command[2:]]
    report: dict[str, Any] = {'schema': 1, 'name': name, 'command': command,
                              'phase': 'create', 'stopped': False}
    def save() -> None:
        (evidence/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    save()
    try:
        created = subprocess.run(command, capture_output=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        report['phase'] = 'create_uncertain'
        save()
        raise RuntimeError('Create result uncertain; retain named probe for reconciliation') from None
    if created.returncode:
        report.update(phase='create_failed', exit_code=created.returncode,
                      stderr=created.stderr[:4096].decode(errors='replace'))
        save()
        raise RuntimeError('Probe create failed; inspect retained result')
    container = created.stdout.decode().strip()
    if not re.fullmatch(r'[0-9a-f]{64}', container):
        report['phase'] = 'create_identity_uncertain'
        save()
        raise RuntimeError('Uncertain owned probe identity')
    report.update(container=container, phase='start')
    save()
    try:
        result = bounded_run(['docker', 'start', '--attach', container], timeout=timeout,
                             stdout_limit=8192, stderr_limit=4096)
        report.update(exit_code=result.returncode, stdout=result.stdout.decode(errors='replace'),
                      stderr=result.stderr.decode(errors='replace'), phase='finished')
    except Exception as exc:
        report.update(phase='start_failed', failure=type(exc).__name__)
        save()
        raise
    finally:
        try:
            state = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', container],
                                   capture_output=True, timeout=10, check=False)
            if state.returncode:
                raise RuntimeError('Owned probe state unavailable; stop is unproved')
            if json.loads(state.stdout)['Running']:
                subprocess.run(['docker', 'kill', container], capture_output=True, timeout=10, check=False)
            state = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', container],
                                   capture_output=True, timeout=10, check=False)
            if state.returncode or json.loads(state.stdout)['Running']:
                raise RuntimeError('Owned probe stop is unproved')
            report.update(state=json.loads(state.stdout), stopped=True)
            save()
            removed = subprocess.run(['docker', 'rm', container], capture_output=True, timeout=10, check=False)
            if removed.returncode:
                raise RuntimeError('Stopped one-shot remains; inspect its retained identity')
        finally:
            save()
    return report
