"""Retained one-level inputs with verified manifests and ordinary child intake."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable
import uuid

import yaml

from library.bundle_worker import POLICY, MAX_SOURCE, MAX_TOTAL
from library.catalog import LibraryError, atomic_yaml, digest, now, sync_dir
from library.readers import fingerprint, sha256, inspect_file, READER_POLICY

RESERVE = 20 * 1024**3


def digest_name(member: str) -> str:
    import hashlib
    return hashlib.sha256(member.encode()).hexdigest()


def verify_directory(directory: Path, parent_hash: str) -> dict[str, Any]:
    if directory.is_symlink() or any(path.is_symlink() for path in directory.parents):
        raise LibraryError('Symlink in retained bundle path')
    manifest = directory / 'manifest.yaml'
    if manifest.is_symlink():
        raise LibraryError('Symlink bundle manifest')
    value = yaml.safe_load(manifest.read_text())
    if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('policy') != POLICY
            or value.get('parent_sha256') != parent_hash or not isinstance(value.get('members'), list)
            or not 1 <= len(value['members']) <= 256):
        raise LibraryError('Invalid retained bundle manifest')
    expected = {'manifest.yaml'}
    names: set[str] = set()
    files: set[str] = set()
    comics = 0
    total = 0
    from pathlib import PurePosixPath
    for item in value['members']:
        if not isinstance(item, dict) or set(item) != {'member', 'size', 'file', 'kind', 'sha256'}:
            raise LibraryError('Invalid retained bundle member')
        member, name, size = item['member'], item['file'], item['size']
        if (not isinstance(member, str) or not member or len(member) > 1024 or
                PurePosixPath(member).is_absolute() or '..' in PurePosixPath(member).parts or
                str(PurePosixPath(member)) != member or '\\' in member or ':' in member or
                any(ord(char) < 32 for char in member) or member.casefold() in names or
                type(size) is not int or size < 0 or not isinstance(item['sha256'], str) or
                not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])):
            raise LibraryError('Invalid or duplicate retained bundle member')
        names.add(member.casefold())
        total += size
        if name is None and item['kind'] == 'ancillary':
            cap = {'ComicInfo.xml': 1024**2, 'Thumbs.db': 4*1024**2, '.picasa.ini': 64*1024}.get(PurePosixPath(member).name, -1)
            if size > cap:
                raise LibraryError('Invalid retained ancillary entry')
            continue
        suffix = PurePosixPath(member).suffix.lower()
        if (item['kind'] != 'comic' or suffix not in ('.pdf', '.cbz', '.cbr') or
                not 0 < size <= MAX_SOURCE or name != digest_name(member) + suffix or name in files):
            raise LibraryError('Invalid derived bundle filename or bounds')
        files.add(name)
        comics += 1
        expected.add(name)
        path = directory / name
        if fingerprint(path)[2] != size or sha256(path) != item['sha256']:
            raise LibraryError('Retained bundle child changed')
    if not 1 <= comics <= 64 or total > MAX_TOTAL or value.get('expanded_size') != total:
        raise LibraryError('Retained bundle expansion or count limit')
    if {path.name for path in directory.iterdir()} != expected:
        raise LibraryError('Unknown retained bundle files')
    return value


def retain(source: Path, state_dir: Path, *, stable_seconds: float = 2,
           fault: Callable[[str], None] = lambda phase: None, wall_limit: float = 180) -> tuple[Path, dict[str, Any]]:
    if not 0 < wall_limit <= 180:
        raise LibraryError('Invalid bundle wall limit')
    source = source.absolute()
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise LibraryError('Symlink bundle source')
    before = fingerprint(source)
    if not 0 < before[2] <= MAX_SOURCE or re.search(r'(?:^|[ ._-])(?:part|pt)[ ._-]?\d+(?:[ ._-]|$)', source.name, re.I):
        raise LibraryError('Bundle source limit or multipart name')
    time.sleep(stable_seconds)
    if fingerprint(source) != before:
        raise LibraryError('Bundle source is unstable')
    parent_hash = sha256(source)
    root = state_dir / 'bundle-inputs'
    if source.is_relative_to(state_dir.absolute()) or state_dir.absolute().is_relative_to(source.parent):
        raise LibraryError('Bundle staging must be outside reception')
    if any(p.is_symlink() for p in (root.absolute(), *root.absolute().parents)):
        raise LibraryError('Symlink bundle state directory')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding = root / ('.source-' + digest(str(source)) + '.yaml')
    source_record = {'sha256': parent_hash, 'fingerprint': list(before)}
    if binding.is_symlink():
        raise LibraryError('Symlink bundle source binding')
    if binding.exists():
        if yaml.safe_load(binding.read_text()) != source_record:
            raise LibraryError('Bundle source changed since extraction intent')
    else:
        atomic_yaml(binding, source_record, exclusive=True)
    target = root / parent_hash
    if target.exists():
        manifest = verify_directory(target, parent_hash)
    else:
        # Interrupted operation-owned directories remain as evidence. Only a
        # completely verified manifest can be reused after a process death.
        complete = []
        for old in root.glob('.' + parent_hash + '-*'):
            if old.is_symlink() or not old.is_dir():
                raise LibraryError('Unknown bundle preparation path')
            if any(p.is_symlink() or not p.is_file() or
                   (p.name != 'manifest.yaml' and not re.fullmatch(r'[0-9a-f]{64}\.(pdf|cbz|cbr)', p.name))
                   for p in old.iterdir()):
                raise LibraryError('Unknown files in interrupted bundle preparation')
            if (old / 'manifest.yaml').exists():
                complete.append(old)
        if len(complete) > 1:
            raise LibraryError('Ambiguous complete bundle preparations')
        if complete:
            stage = complete[0]
            manifest = verify_directory(stage, parent_hash)
        else:
            if shutil.disk_usage(root).free - MAX_TOTAL < RESERVE:
                raise LibraryError('Bundle extraction cannot retain 20 GiB reserve')
            stage = root / ('.' + parent_hash + '-' + uuid.uuid4().hex)
            stage.mkdir(mode=0o700)
            fault('intent')
            from library.sandbox import native_prefix, PYTHON
            command = native_prefix(source, output=stage) + [PYTHON, '-m', 'library.bundle_worker', '/tmp/input', '/tmp/output']
            image = os.environ.get('BOOKSYS_READER_IMAGE')
            container_name = 'booksys-bundle-' + uuid.uuid4().hex if image else None
            if image:
                if not re.fullmatch(r'[a-z0-9./:_@-]+', image) or any(c in str(source) + str(stage.absolute()) for c in ',\n'):
                    raise LibraryError('Unsafe bundle reader image or mount')
                command = ['docker', 'create', '--rm', '--name', str(container_name), '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
                    '--memory', '768m', '--cpus', '1', '--pids-limit', '32',
                    '--mount', f'type=bind,src={source},dst=/input,readonly',
                    '--mount', f'type=bind,src={stage.absolute()},dst=/output',
                    '--entrypoint', '/app/.venv/bin/python', image, '-m', 'library.bundle_worker', '/input', '/output']
                from library.process import bounded_run
                created = bounded_run(command, timeout=20, stdout_limit=8192)
                if created.returncode:
                    raise LibraryError('Cannot create isolated bundle engine')
                command = ['docker', 'start', '--attach', str(container_name)]
            try:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError:
                if container_name:
                    stop_bundle_container(container_name)
                raise
            started = time.monotonic()
            try:
                while True:
                    try:
                        stdout, _ = process.communicate(timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        if shutil.disk_usage(root).free < RESERVE:
                            raise LibraryError('Bundle extraction reached disk reserve')
                        if time.monotonic() - started > wall_limit:
                            raise LibraryError('Bundle extraction exceeded wall limit')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                    if container_name:
                        stop_bundle_container(container_name)
            if process.returncode:
                raise LibraryError('Isolated bundle worker failed')
            manifest = json.loads(stdout)
            if manifest.get('status') != 'extracted' or manifest.get('parent_sha256') != parent_hash:
                raise LibraryError(manifest.get('reason', 'Bundle source hash mismatch'))
            atomic_yaml(stage / 'manifest.yaml', manifest)
            verify_directory(stage, parent_hash)
            fault('prepared')
        if fingerprint(source) != before or sha256(source) != parent_hash:
            raise LibraryError('Bundle source changed during extraction')
        os.rename(stage, target)
        sync_dir(root)
        fault('retained')
    if fingerprint(source) != before or sha256(source) != parent_hash:
        raise LibraryError('Bundle source changed during retention')
    return target, manifest


def receive_bundle(state: dict[str, Any], state_dir: Path, source: Path, oid: str,
                   origin: dict[str, Any], hint: dict[str, Any], *, budget: int,
                   stable_seconds: float = 2) -> int:
    previous = state['origins'].get(oid, {})
    stamp = list(fingerprint(source))
    if previous.get('bundle') and (previous.get('fingerprint') != stamp or sha256(source) != previous['sha256']):
        raise LibraryError('Bundle parent changed after retention')
    directory, manifest = retain(source, state_dir, stable_seconds=stable_seconds)
    parent_hash = manifest['parent_sha256']
    if previous.get('bundle') and previous['bundle']['members'] != manifest['members']:
        raise LibraryError('Retained bundle manifest changed from canonical parent')
    if previous.get('sha256') and previous['sha256'] != parent_hash:
        raise LibraryError('Bundle parent hash changed')
    origin.update(sha256=parent_hash, availability='bundle', fingerprint=stamp,
                  bundle={'policy': POLICY, 'members': manifest['members'], 'expanded_size': manifest['expanded_size']},
                  reason='Bundle parent; each complete member is reviewed separately')
    state['origins'][oid] = origin
    inspected = 0
    for member in manifest['members']:
        if member['kind'] != 'comic':
            continue
        cid = digest({'parent_origin': oid, 'member': member['member']})
        old = state['origins'].get(cid, {})
        child = {**old, 'source': origin['source'], 'pid': origin['pid'], 'fid': origin['fid'],
                 'parent_origin': oid, 'parent_sha256': parent_hash, 'member': member['member'],
                 'member_sha256': member['sha256'], 'size': member['size'],
                 'path': str(Path('bundle-inputs') / parent_hash / member['file']),
                 'sha256': member['sha256'], 'observed_at': now(), 'transfer_status': 0,
                 'transfer_error': origin.get('transfer_error', False)}
        if old.get('availability') == 'validated' and member['sha256'] not in state['assets']:
            raise LibraryError('Validated bundle child references missing asset')
        temporary = any(fragment in old.get('reason', '').lower() for fragment in
                        ('isolated reader failed', 'resource limit exceeded', 'timed out'))
        retry = temporary and int(old.get('intake_attempts', 1)) < 3 and old.get('intake_next_attempt', 0) <= time.time()
        needs_validation = (not old or old.get('reason') == 'Intake budget deferred' or retry or
                            old.get('validation_policy') != READER_POLICY)
        if needs_validation:
            if inspected >= budget:
                child.update(availability='pending', reason='Intake budget deferred')
            else:
                inspected += 1
                result = inspect_file(directory / member['file'], stable_seconds=stable_seconds)
                if result.get('sha256') and result['sha256'] != member['sha256']:
                    raise LibraryError('Derived input changed during validation')
                child.update(availability=result['status'], reason=result.get('reason', ''), reader_policy=result.get('reader_policy'), validation_policy=READER_POLICY,
                             intake_attempts=int(old.get('intake_attempts', 0)) + 1, intake_next_attempt=time.time() + 300)
                if result['status'] == 'validated':
                    asset = state['assets'].setdefault(member['sha256'], {**result, 'origins': [],
                        'assertions': [], 'overrides': {}, 'disposition': 'pending', 'captured_hints': {}})
                    if not asset['assertions']:
                        for field, xml_field in (('genres', 'Genre'), ('public', 'AgeRating')):
                            raw = result.get('embedded', {}).get(xml_field)
                            if raw:
                                asset['assertions'].append({'field': field, 'raw': raw,
                                    'normalized': [v.strip() for v in raw.split(',')] if field == 'genres' else raw,
                                    'source': 'embedded', 'upstream': 'embedded-unverified',
                                    'url': 'sha256:' + member['sha256'], 'retrieved_at': now(), 'validation': 'asserted'})
                    if cid not in asset['origins']:
                        asset['origins'].append(cid)
                    # Filename is a discovery hint, never the parent's bibliography.
                    asset['captured_hints'][origin['source'] + '#bundle-member=' + cid] = {
                        'title': Path(member['member']).stem, 'parent_source': origin['source'], 'parent_title': hint.get('title')}
        state['origins'][cid] = child
    return inspected


def stop_bundle_container(name: str) -> None:
    """Stop and read back only this extraction's owned engine."""
    from library.process import bounded_run
    if not re.fullmatch(r'booksys-bundle-[0-9a-f]{32}', name):
        raise LibraryError('Invalid owned bundle container')
    result = bounded_run(['docker', 'stop', '--time', '1', name], timeout=15, stdout_limit=8192)
    if result.returncode and b'no such' not in result.stderr.lower():
        raise LibraryError('Owned bundle engine stop failed; preserve evidence')
    for attempt in range(20):
        result = bounded_run(['docker', 'inspect', '--format', '{{.State.Running}}', name], timeout=10, stdout_limit=8192)
        if (result.returncode == 0 and result.stdout.strip() == b'false') or (result.returncode and b'no such' in result.stderr.lower()):
            return
        if result.returncode or result.stdout.strip() != b'true':
            break
        time.sleep(.1)
    raise LibraryError('Owned bundle engine termination is not proved')
