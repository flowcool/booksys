"""Retained one-level inputs with verified manifests and ordinary child intake."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Callable
import uuid

import yaml

from library.bundle_worker import POLICY, MAX_SOURCE, MAX_CHILD, MAX_TOTAL
from library.relocation import matches_retained
from library.primitives import LibraryError, atomic_yaml, digest, now, sync_dir
from library.readers import fingerprint, sha256, inspect_file, READER_POLICY



def digest_name(member: str) -> str:
    import hashlib
    return hashlib.sha256(member.encode()).hexdigest()


def verify_directory(directory: Path, parent_hash: str) -> dict[str, Any]:
    if directory.is_symlink() or any(path.is_symlink() for path in directory.parents):
        raise LibraryError('Symlink in retained bundle path')
    manifest = directory / 'manifest.yaml'
    if manifest.is_symlink():
        raise LibraryError('Symlink bundle manifest')
    manifest_time = manifest.stat().st_mtime_ns          # a child written before the manifest and unchanged is not hashed again
    value = yaml.safe_load(manifest.read_text())
    if (not isinstance(value, dict) or value.get('schema') != 1 or (value.get('policy') != POLICY and not (value.get('policy') == 'comic-bundle-1' and not value.get('source_parts')))
            or value.get('parent_sha256') != parent_hash or not isinstance(value.get('members'), list)
            or not 1 <= len(value['members']) <= 256):
        raise LibraryError('Invalid retained bundle manifest')
    if value.get('source_parts') and value.get('volume_end_verified') is not True:
        raise LibraryError('Multipart container-end proof is missing')
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
                not 0 < size <= MAX_CHILD or name != digest_name(member) + suffix or name in files):
            raise LibraryError('Invalid derived bundle filename or bounds')
        files.add(name)
        comics += 1
        expected.add(name)
        path = directory / name
        info = fingerprint(path)
        if info[2] != size or (info[3] > manifest_time and sha256(path) != item['sha256']):
            raise LibraryError('Retained bundle child changed')
    if not 1 <= comics <= 64 or total > MAX_TOTAL or value.get('expanded_size') != total:
        raise LibraryError('Retained bundle expansion or count limit')
    if {path.name for path in directory.iterdir()} != expected:
        raise LibraryError('Unknown retained bundle files')
    return value


_RETAINED: dict[tuple[Any, ...], tuple[Path, dict[str, Any]]] = {}


def retain(source: Path, state_dir: Path, *, stable_seconds: float = 2,
           fault: Callable[[str], None] = lambda phase: None,
           parts: list[Path] | None = None, detached: bool = False) -> tuple[Path, dict[str, Any]]:
    source = source.absolute()
    parts = [part.absolute() for part in parts] if parts else None
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise LibraryError('Symlink bundle source')
    inputs = parts or [source]
    if parts and (source != parts[0] or len({p.name for p in parts}) != len(parts)):
        raise LibraryError('Invalid multipart input set')
    if parts:
        from library.multipart_model import select_parts
        synthetic = [{'fid': i, 'name': part.name, 'status': 0, 'url': 'https://isolated.invalid/' + str(i),
                      'input_folder': str(part.parent)} for i,part in enumerate(parts)]
        if [item['name'] for item in select_parts(synthetic, synthetic[0])] != [part.name for part in parts]:
            raise LibraryError('Unsupported multipart order')
    for part in inputs:
        if any(p.is_symlink() for p in (part, *part.parents)):
            raise LibraryError('Symlink multipart source')
    stamps = [fingerprint(part) for part in inputs]
    # Intake calls retain for every bundle at every sweep. A bundle whose source files (device, inode, size, mtime, ctime)
    # did not change since the last full verification in this process is not hashed again: the repeated hashing of 18 GB
    # kept the intake worker busy for minutes per sweep (py-spy: sha256 under retain and verify_directory).
    memo = (str(source), tuple(stamps), str(state_dir), bool(parts), detached)
    known = _RETAINED.get(memo)
    if known is not None and known[0].is_dir():
        return known
    before = stamps[0]
    if not 0 < sum(stamp[2] for stamp in stamps) <= MAX_SOURCE:
        raise LibraryError('Bundle source limit: 4 GiB source set maximum')
    if not parts and re.search(r'(?:^|[ ._-])(?:part|pt)[ ._-]?\d+(?:[ ._-]|$)', source.name, re.I):
        raise LibraryError('Unsupported multipart name; a tracked complete volume set is required')
    time.sleep(stable_seconds)
    if [fingerprint(part) for part in inputs] != stamps:
        raise LibraryError('Bundle source is unstable')
    hashes = None
    if not parts:
        # The source binding written at the first extraction holds its hash and its physical identity (device, inode,
        # size, mtime, ctime): an unchanged identity is the same bytes, no need to read the whole file again.
        from library.media_paths import bundle_inputs as _inputs
        _root = state_dir/'bundle-inputs' if detached else _inputs(state_dir)
        _binding = _root / ('.source-' + digest(str(source)) + '.yaml')
        if _binding.is_file() and not _binding.is_symlink():
            _kept = yaml.safe_load(_binding.read_text()) or {}
            if _kept.get('fingerprint') == list(stamps[0]) and isinstance(_kept.get('sha256'), str):
                hashes = [_kept['sha256']]
    if hashes is None:
        hashes = [sha256(part) for part in inputs]
    parent_hash = hashes[0]
    source_parts = [{'name': part.name, 'sha256': key, 'size': stamp[2]}
                    for part,key,stamp in zip(inputs,hashes,stamps)]
    from library.media_paths import bundle_inputs
    root = state_dir/'bundle-inputs' if detached else bundle_inputs(state_dir)
    if source.is_relative_to(state_dir.absolute()) or state_dir.absolute().is_relative_to(source.parent):
        raise LibraryError('Bundle staging must be outside reception')
    if any(p.is_symlink() for p in (root.absolute(), *root.absolute().parents)):
        raise LibraryError('Symlink bundle state directory')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding = root / ('.source-' + digest(str(source)) + '.yaml')
    source_record = {'sha256': parent_hash, 'fingerprint': list(before)}
    if parts:
        source_record['source_parts'] = source_parts
    if binding.is_symlink():
        raise LibraryError('Symlink bundle source binding')
    if binding.exists():
        retained = yaml.safe_load(binding.read_text())
        same_parts = retained.get('source_parts') == source_record.get('source_parts')
        if (retained.get('sha256') != parent_hash or not same_parts
                or not matches_retained(source, retained.get('fingerprint', []), parent_hash)):
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
            stage = root / ('.' + parent_hash + '-' + uuid.uuid4().hex)
            stage.mkdir(mode=0o700)
            fault('intent')
            from library.decode import run
            manifest = run('bundle', source, {'parts': [str(p.absolute()) for p in inputs]} if parts else {}, stage)
            if manifest.get('status') == 'pending':
                shutil.rmtree(stage)
                return stage, manifest
            if manifest.get('status') != 'extracted' or manifest.get('parent_sha256') != parent_hash:
                raise LibraryError(manifest.get('reason', 'Bundle source hash mismatch'))
            if parts:
                manifest['source_parts'] = source_parts
            atomic_yaml(stage / 'manifest.yaml', manifest)
            verify_directory(stage, parent_hash)
            fault('prepared')
        if [fingerprint(part) for part in inputs] != stamps:
            raise LibraryError('Bundle source changed during extraction')
        os.rename(stage, target)
        sync_dir(root)
        fault('retained')
    if [fingerprint(part) for part in inputs] != stamps:
        raise LibraryError('Bundle source changed during retention')
    if parts and manifest.get('source_parts') != source_parts:
        raise LibraryError('Multipart source manifest changed')
    _RETAINED[memo] = (target, manifest)
    return target, manifest


def receive_bundle(state: dict[str, Any], state_dir: Path, source: Path, oid: str,
                   origin: dict[str, Any], hint: dict[str, Any], *, save: Callable[[], None],
                   stable_seconds: float = 2, parts: list[Path] | None = None, detached: bool = False) -> None:
    previous = state['origins'].get(oid, {})
    stamp = list(fingerprint(source))
    if previous.get('bundle') and (not matches_retained(source, previous.get('fingerprint', []), previous['sha256']) or sha256(source) != previous['sha256']):
        raise LibraryError('Bundle parent changed after retention')
    directory, manifest = retain(source, state_dir, stable_seconds=stable_seconds, parts=parts, detached=detached)
    if manifest.get('status') == 'pending':
        origin.update(availability='pending', reason=manifest['reason'])
        state['origins'][oid] = origin
        return
    parent_hash = manifest['parent_sha256']
    if parts:
        expected_parts = [{'name': Path(part['path']).name, 'sha256': part['sha256'],
                           'size': part['fingerprint'][2]} for part in origin['source_parts']]
        if manifest.get('source_parts') != expected_parts:
            raise LibraryError('Multipart source changed during retention')
    if parts:
        retained_parts = previous.get('source_parts')
        if retained_parts:
            current_parts = origin.get('source_parts', [])
            if (len(retained_parts) != len(current_parts) or len(parts) != len(retained_parts)
                    or any({k: v for k, v in old.items() if k != 'fingerprint'} !=
                           {k: v for k, v in new.items() if k != 'fingerprint'}
                           or not matches_retained(path, old['fingerprint'], old['sha256'])
                           for old, new, path in zip(retained_parts, current_parts, parts))):
                raise LibraryError('Multipart source provenance changed')
            origin['source_parts'] = retained_parts  # Preserve the original proof.
    if previous.get('bundle') and previous['bundle']['members'] != manifest['members']:
        raise LibraryError('Retained bundle manifest changed from canonical parent')
    if previous.get('sha256') and previous['sha256'] != parent_hash:
        raise LibraryError('Bundle parent hash changed')
    origin.update(sha256=parent_hash, availability='bundle', fingerprint=stamp,
                  bundle={'policy': POLICY, 'members': manifest['members'], 'expanded_size': manifest['expanded_size']},
                  reason='Bundle parent; each complete member is reviewed separately')
    state['origins'][oid] = origin
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
        needs_validation = (not old or old.get('availability') == 'pending' or retry or
                            old.get('validation_policy') != READER_POLICY)
        if needs_validation:
            result = inspect_file(directory / member['file'], stable_seconds=stable_seconds)
            if result.get('reason') == 'Stage enclosing deadline exceeded':
                raise RuntimeError(result['reason'])
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
        save()
