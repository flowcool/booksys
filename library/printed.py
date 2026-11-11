"""Printed evidence stays separate from embedded metadata and manual decisions."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from library.bibliographic import isbn13, normalized
from library.primitives import now, LibraryError
from library.process import bounded_run


from library.printed_model import printed_isbns, words, identify_printed


def read_printed(path: Any, inspection: dict[str, Any]) -> dict[str, Any]:
    """Read eight validated archive pages in a separate networkless worker."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from library.primitives import LibraryError
    from library.readers import fingerprint, sha256
    path = Path(path).absolute()
    if (any(p.is_symlink() for p in (path, *path.parents)) or
            inspection.get('status') != 'validated' or inspection.get('format') not in ('cbz', 'cbr', 'pdf')):
        raise LibraryError('Printed evidence requires a validated comic input')
    before = fingerprint(path)
    expected = inspection['sha256']
    if sha256(path) != expected:
        raise LibraryError('Printed evidence source changed')
    manifest = inspection.get('page_manifest', [])
    selected = list({p['name']: p for p in [*manifest[:4], *manifest[-4:]]}.values())
    if inspection['format'] == 'pdf':
        count = inspection['pages']
        selected = [{'page': i} for i in sorted(set([*range(1, min(4, count) + 1), *range(max(1, count - 3), count + 1)]))]
    if not selected:
        raise LibraryError('Printed evidence requires a validated page manifest')
    from library.sandbox import native_prefix, PYTHON
    command = native_prefix(path, ocr=True) + [PYTHON, '-m', 'library.printed_worker',
                                             '/tmp/input', '.' + inspection['format']]
    image = os.environ.get('BOOKSYS_READER_IMAGE')
    container_name = None
    if image:
        import uuid
        container_name = 'booksys-ocr-' + uuid.uuid4().hex
        if not re.fullmatch(r'[a-z0-9./:_@-]+', image) or ',' in str(path) or '\n' in str(path):
            raise LibraryError('Invalid printed reader image/bind')
        command = ['docker', 'run', '--rm', '-i', '--name', container_name, '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', f'{os.getuid()}:{os.getgid()}', '--memory', '1536m', '--cpus', '1',
            '--pids-limit', '32', '--mount', f'type=bind,src={path},dst=/input,readonly',
            '--entrypoint', '/app/.venv/bin/python', image, '-m', 'library.printed_worker',
            '/input', '.' + inspection['format']]
    try:
        from library.reader_client import endpoint, execute
        if endpoint():
            process = subprocess.CompletedProcess(command, 0, json.dumps(execute('ocr', path, {'pages': selected})).encode(), b'')
        else:
            process = bounded_run(command, input=json.dumps({'pages': selected}).encode(), timeout=180)
    except BaseException as exc:
        if container_name:
            stop_owned_container(container_name)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise LibraryError('Printed reader exceeded wall time; owned engine stopped') from exc
        raise
    if fingerprint(path) != before or sha256(path) != expected:
        raise LibraryError('Printed evidence source changed during OCR')
    if process.returncode in (125, 126, 127):
        from library.readers import ReaderUnavailable
        raise ReaderUnavailable('Printed reader execution dependency unavailable')
    if process.returncode or len(process.stdout) > 1024**2:
        raise LibraryError('Printed reader failed or exceeded resource limits')
    result: dict[str, Any] = json.loads(process.stdout)
    if result.pop('input_sha256', None) != expected:
        raise LibraryError('Printed reader inspected another source')
    if result.get('status') == 'ok':
        if inspection['format'] == 'pdf':
            if [p.get('page') for p in result['pages']] != [p['page'] for p in selected]:
                raise LibraryError('Printed PDF page selection changed')
        elif [(p['name'], p['sha256']) for p in result['pages']] != [(p['name'], p['sha256']) for p in selected]:
            raise LibraryError('Printed reader page evidence changed')
    result['asset'] = expected
    return result


def prepare_printed(store: Any, asset_id: str, reception: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    from library.primitives import LibraryError
    from pathlib import Path
    from library.origins import resolve_asset
    from activity import report
    report('local', 'working', 'OCR', item=asset_id)
    state = store.result_set('identify', {'assets': [asset_id]})
    asset = state['assets'][asset_id]
    result = read_printed(resolve_asset(store, Path(reception), state, asset_id), asset)
    asset.setdefault('printed_attempts', []).append({k: v for k, v in result.items() if k != 'pages'})
    asset['printed_attempts'] = asset['printed_attempts'][-10:]
    if result['status'] == 'ok':
        asset['printed'] = result
    report('local', 'sleeping' if result['status'] == 'ok' else 'waiting', 'OCR result', item=asset_id, progressed=result['status'] == 'ok')
    return state, result


def scan_printed(store: Any, asset_id: str, reception: Any) -> dict[str, Any]:
    state, result = prepare_printed(store, asset_id, reception)
    store.save(state, expected=state['revision'])
    return result


def stop_owned_container(name: str) -> None:
    """Stop only this controller invocation's named one-shot and verify exit."""
    if not re.fullmatch(r'booksys-ocr-[0-9a-f]{32}', name):
        raise LibraryError('Invalid owned OCR container name')
    stopped = bounded_run(['docker', 'stop', '--time', '1', name], timeout=15, stdout_limit=8192)
    if stopped.returncode and b'No such container' not in stopped.stderr:
        raise LibraryError(f'Cannot stop owned OCR engine {name}; preserve its evidence')
    state = bounded_run(['docker', 'inspect', '--format', '{{.State.Running}}', name],
                        timeout=10, stdout_limit=8192)
    if state.returncode == 0 and state.stdout.strip() == b'false':
        return
    if state.returncode != 0 and b'No such' in state.stderr:
        return  # --rm already removed this completed one-shot.
    raise LibraryError(f'Owned OCR engine {name} termination is not proved')
