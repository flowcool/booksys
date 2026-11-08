"""Journaled independent copies. Linux atomic directory publication only."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import html
import json
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from library.catalog import Catalog, LibraryError, atomic_yaml, digest, lock, now, sync_dir
from library.intake import reception_path
from library.readers import fingerprint, sha256
from library.transform import transform_archive, VERSION
from library.xmlmetadata import comicinfo


def rename_atomic(source: Path, target: Path, *, exchange: bool = False) -> None:
    """RENAME_NOREPLACE for creation; RENAME_EXCHANGE for full-folder updates."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'renameat2', None)
    if function is None:
        raise LibraryError('renameat2 is required; no unsafe rename fallback')
    result = function(-100, os.fsencode(source), -100, os.fsencode(target), 2 if exchange else 1)
    if result:
        code = ctypes.get_errno()
        raise LibraryError(f'Atomic directory rename failed: {os.strerror(code)}')
    sync_dir(source.parent)
    if target.parent != source.parent:
        sync_dir(target.parent)


def manifest(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_dir():
        raise LibraryError('Managed directory missing or symlink')
    output = {}
    for child in path.iterdir():
        if child.is_symlink() or not child.is_file():
            raise LibraryError('Unmanaged directory content')
        output[child.name] = sha256(child)
    return output


def verify(path: Path, expected: dict[str, str]) -> None:
    if manifest(path) != expected:
        raise LibraryError('Managed file drift; preserve folder and investigate')


def private_tree(path: Path) -> None:
    """Only the dedicated writer can mutate this tree; readers require RO access."""
    for entry in (path, *path.rglob('*')):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise LibraryError('Exclusive library ownership is not established')


class Capacity:
    def __init__(self, *, reserve: int = 20 * 1024 ** 3,
                 free: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
                 external_active: Callable[[], bool] = lambda: False,
                 max_external_bps: int | None = None, qualified: bool = False,
                 max_interval: float = .25):
        self.reserve, self.free, self.external_active = reserve, free, external_active
        self.max_external_bps, self.qualified, self.max_interval = max_external_bps, qualified, max_interval
        self.last_check = time.monotonic()

    def check(self, path: Path, remaining: int) -> None:
        active = self.external_active()
        margin = 0
        if active:
            if not self.qualified or self.max_external_bps is None:
                raise LibraryError('Concurrent writer capacity envelope is not qualified; promotion suspended')
            elapsed = time.monotonic() - self.last_check
            if elapsed > self.max_interval:
                raise LibraryError('Capacity probe interval exceeded qualified envelope')
            margin = int(self.max_external_bps * self.max_interval)
        self.last_check = time.monotonic()
        if self.free(path) < self.reserve + remaining + margin:
            raise LibraryError('Insufficient capacity to preserve 20 GiB reserve')


def fiche_for(state: dict[str, Any], asset_id: str) -> bytes:
    asset = state['assets'][asset_id]
    identity = asset['identity']
    work = state['works'][identity['work']]
    edition = state['editions'][identity['edition']]
    fields = {**work, **asset.get('overrides', {})}
    lines = ['# ' + html.escape(str(fields.get('title', 'Unknown'))), '',
             'Work: ' + identity['work'], 'Edition: ' + identity['edition'],
             'Language: ' + html.escape(str(edition['language'])),
             'Publisher: ' + html.escape(str(edition['publisher'])),
             'Asset SHA-256: ' + asset_id,
             'Cover: local first-page preview' if asset.get('cover') else 'Cover: unavailable', '',
             '## Validated identity and operator overrides', '', '<pre>' + html.escape(json.dumps(
                 {'work': fields, 'edition': {k: v for k, v in edition.items() if k not in ('publication', 'selected')},
                  'overrides': asset.get('overrides', {})}, ensure_ascii=False, indent=2)) + '</pre>', '',
             '## Source assertions (untrusted; conflicts retained)', '', '<pre>' + html.escape(json.dumps(
                 asset.get('assertions', []), ensure_ascii=False, indent=2)) + '</pre>', '',
             '## Captured embedded metadata (untrusted assertions)', '', '<pre>' + html.escape(json.dumps(
                 asset.get('embedded', {}), ensure_ascii=False, indent=2)) + '</pre>', '',
             '## Origins', '', '<pre>' + html.escape(json.dumps(
                 [{k: v for k, v in state['origins'][key].items() if k not in ('observed_at', 'transfer_status')} for key in asset['origins']], ensure_ascii=False, indent=2)) + '</pre>', '']
    return '\n'.join(lines).encode()


class Publisher:
    def __init__(self, store: Catalog, reception: Path, root: Path, *, isolated: bool = False, lab: bool = False,
                 capacity: Capacity | None = None,
                 fault: Callable[[str, dict[str, Any]], None] = lambda *_: None,
                 exclusive: bool = False):
        self.store, self.reception, self.root = store, reception.resolve(), root.resolve()
        self.isolated, self.capacity, self.fault = isolated, capacity or Capacity(), fault
        if self.root.is_relative_to(self.reception) or self.reception.is_relative_to(self.root):
            raise LibraryError('Library and reception must be disjoint')
        if isolated and (self.root.is_relative_to(Path('/data/booksys')) or
                         self.reception.is_relative_to(Path('/data/booksys'))):
            raise LibraryError('Isolated mode refuses live Booksys data')
        if lab:
            from library.runtime import lab_preflight
            if isolated:
                raise LibraryError('Lab and isolated modes are mutually exclusive')
            lab_preflight(reception, root, store.directory)
        if not (isolated or lab) and not exclusive:
            raise LibraryError('Runtime preflight must establish dedicated writer exclusivity')
        self.exposed, self.staging, self.recovery = (self.root / name for name in ('bd', '.staging', '.recovery'))
        self.journals = store.directory / 'library-operations'
        for directory in (self.root, self.exposed, self.staging, self.recovery, self.journals):
            if directory.is_symlink():
                raise LibraryError('Symlink in library layout')
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if len({d.stat().st_dev for d in (self.exposed, self.staging, self.recovery)}) != 1:
            raise LibraryError('Library staging and recovery must share the exposure filesystem')
        private_tree(self.root)

    def transition(self, operation: dict[str, Any], phase: str) -> None:
        operation['phase'] = phase
        atomic_yaml(self.journals / (operation['id'] + '.yaml'), operation)
        self.fault(phase, operation)

    def check_capacity(self, operation: dict[str, Any], remaining: int) -> None:
        try:
            self.capacity.check(self.root, remaining)
        except LibraryError:
            for stage, allowed in ((self.staging / operation['id'], set(operation['manifest'])),
                                   (self.staging / (operation['id'] + '-transform'), {'book.cbz'})):
                if stage.exists():
                    contents = manifest(stage)
                    if set(contents) - allowed:
                        raise LibraryError('Unknown temporary content; capacity cleanup blocked')
                    operation.setdefault('aborted_partial_manifests', {})[stage.name] = contents
                    for name in contents:
                        (stage / name).unlink()
                    stage.rmdir()
                    sync_dir(self.staging)
            self.transition(operation, 'capacity_aborted')
            raise

    def resolve_origin(self, state: dict[str, Any], asset_id: str) -> Path:
        from library.origins import resolve_asset
        return resolve_asset(self.store, self.reception, state, asset_id)

    def publish(self, asset_id: str, *, replace: bool = False, decision: str | None = None, role: str | None = None) -> dict[str, Any]:
        with lock(self.store.directory / 'library-copy.lock', blocking=False):
            self.reconcile(locked=True)
            self.store.replay()
            state = self.store.read()
            self.decision_gate(state, asset_id, decision, role)
            asset = state['assets'][asset_id]
            identity = asset.get('identity')
            if not identity or asset.get('contested') or asset.get('disposition') in ('exclude', 'defer'):
                raise LibraryError('Identity, edition, or operator disposition blocks promotion')
            if asset.get('status') != 'validated':
                raise LibraryError('Unvalidated asset')
            edition = state['editions'][identity['edition']]
            if edition.get('selected') != identity['variant']:
                raise LibraryError('Asset is not the selected edition variant')
            claims = {v['edition'] for v in state['variants'].values() if v['asset'] == asset_id and v.get('active', True)}
            if claims != {identity['edition']}:
                raise LibraryError('Conflicting asset ownership')
            from library.origins import resolve_asset_origin
            source_origin, source = resolve_asset_origin(self.store, self.reception, state, asset_id)
            fiche = fiche_for(state, asset_id)
            cover = base64.b64decode(asset['cover'], validate=True) if asset.get('cover') else None
            xml = comicinfo(state, asset_id)
            book_name = 'book.cbz' if xml else 'book.pdf'
            export_version = digest({'asset': asset_id, 'xml': hashlib.sha256(xml).hexdigest() if xml else None,
                                     'fiche': hashlib.sha256(fiche).hexdigest(), 'cover': asset.get('cover'), 'tool': VERSION})
            expected = {book_name: asset_id if xml is None else '', 'fiche.md': hashlib.sha256(fiche).hexdigest()}
            if cover:
                expected['couverture.png'] = hashlib.sha256(cover).hexdigest()
            previous = edition.get('publication')
            if previous:
                target = self.exposed / previous['target']
                verify(target, previous['manifest'])
                if previous.get('export_version') == export_version and previous['asset'] == asset_id:
                    return self.load_operation(previous['operation'])
                if not replace:
                    raise LibraryError('An explicit replacement operation is required')
            else:
                slug = re.sub(r'[^a-z0-9]+', '-', str(state['works'][identity['work']]['title']).lower()).strip('-')[:48] or 'work'
                target = self.exposed / (slug + '--' + identity['edition'])
                if target.exists() or target.is_symlink():
                    raise LibraryError('Unmanaged publication destination')
            private_tree(self.root)
            estimated = ((2 * 1024**3 if asset['format'] == 'pdf' else
                          sum(page['size'] for page in asset.get('page_manifest', []))) + 8 * 1024**2) + len(fiche) + (len(cover) if cover else 0)
            self.capacity.check(self.root, estimated)
            operation = {'schema': 1, 'id': uuid.uuid4().hex, 'kind': 'replace' if previous else 'create',
                'asset': asset_id, 'edition': identity['edition'], 'target': target.name,
                'manifest': expected, 'previous': previous, 'catalogue_revision': state['revision'],
                'source': source_origin, 'created_at': now(), 'export_version': export_version,
                'artifact_format': 'cbz' if xml else 'pdf', 'decision': decision, 'role': role}
            self.transition(operation, 'intent')
            stage = self.staging / operation['id']
            stage.mkdir(mode=0o700)
            before = fingerprint(source)
            if xml:
                self.transition(operation, 'transforming')
                transformed = self.staging / (operation['id'] + '-transform')
                transformed.mkdir(mode=0o700)
                result = transform_archive(source, transformed, xml, asset_id,
                    check_capacity=lambda: self.check_capacity(operation, len(fiche) + (len(cover) if cover else 0)))
                if (asset['format'] != 'pdf' and result['page_manifest'] != asset.get('page_manifest')) or (
                        asset['format'] == 'pdf' and result['pages'] != asset['pages']):
                    raise LibraryError('Transformed page hashes/order differ from identified asset')
                rename_atomic(transformed / 'book.cbz', stage / book_name)
                transformed.rmdir()
                expected[book_name] = result['artifact_sha256']
                operation['transformation'] = {k: result[k] for k in ('transform_version', 'xml_sha256',
                    'source_asset_sha256', 'artifact_sha256', 'page_manifest', 'ancillary_manifest', 'tool_versions')}
                if 'page_methods' in result:
                    operation['transformation']['page_methods'] = result['page_methods']
                self.transition(operation, 'transformed')
                self.fault('copying', operation)
            else:
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as incoming, (stage / book_name).open('xb') as output:
                    os.fchmod(output.fileno(), 0o600)
                    remaining = asset['size']
                    while chunk := incoming.read(1024 * 1024):
                        self.check_capacity(operation, remaining + len(fiche) + (len(cover) if cover else 0))
                        output.write(chunk)
                        remaining -= len(chunk)
                        self.fault('copying', operation)
                    output.flush()
                    os.fsync(output.fileno())
            if fingerprint(source) != before or sha256(source) != asset_id or sha256(stage / book_name) != expected[book_name]:
                raise LibraryError('Original changed between identification and copy')
            for name, data in [('fiche.md', fiche), *([('couverture.png', cover)] if cover else [])]:
                with (stage / name).open('xb') as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            sync_dir(stage)
            sync_dir(self.staging)
            self.transition(operation, 'prepared')
            verify(stage, expected)
            self.check_capacity(operation, 0)
            self.transition(operation, 'verified')
            existing_artifact = self.store.read()['artifacts'].get(expected[book_name])
            if existing_artifact and identity['edition'] not in existing_artifact['editions'] and not self.artifact_transfer_allowed(state, asset_id, decision):
                raise LibraryError('Generated artifact has conflicting edition ownership')
            self.fault('before_substitution', operation)
            if self.store.read()['revision'] != operation['catalogue_revision']:
                raise LibraryError('Catalogue changed while preparing publication')
            if previous:
                verify(target, previous['manifest'])
            private_tree(self.root)
            rename_atomic(stage, target, exchange=bool(previous))
            self.fault('after_substitution', operation)
            if previous:
                # A dedicated writer is a prerequisite. This second check also
                # detects a raced external edit; it does not make rename a CAS.
                try:
                    verify(stage, previous['manifest'])
                except LibraryError:
                    verify(target, expected)
                    rename_atomic(stage, target, exchange=True)
                    self.transition(operation, 'blocked')
                    raise
                rename_atomic(stage, self.recovery / operation['id'])
            self.transition(operation, 'published')
            self.commit(operation)
            return operation

    def artifact_transfer_allowed(self, state: dict[str, Any], asset: str, decision: str | None) -> bool:
        return bool(decision and any(op.get('decision') == decision and op.get('role') == 'withdraw'
                    and op['status'] == 'committed' for op in state['operations'].values())
                    and not any(e.get('publication', {}).get('asset') == asset for e in state['editions'].values()))

    def decision_gate(self, state: dict[str, Any], asset: str, decision: str | None, role: str | None) -> None:
        from library.resolution import gate
        gate(state, asset, decision, role)
        if decision:
            record = next((r for r in self.store.ledger() if r['id'] == decision and r['action'] == 'resolve'), None)
            if not record or record['target'] != asset or role not in record['roles']:
                raise LibraryError('Operation is not authorized by this decision')
            if any(op.get('decision') == decision and op.get('role') == role and op['status'] == 'committed'
                   for op in state['operations'].values()):
                raise LibraryError('Decision role already committed')

    def preflight(self, asset_id: str) -> None:
        """Prove deterministic transform and capacity prerequisites before withdrawal."""
        state = self.store.read()
        source = self.resolve_origin(state, asset_id)
        identity = state['assets'][asset_id]['identity']
        title = state['works'][identity['work']]['title']
        slug = re.sub(r'[^a-z0-9]+', '-', str(title).lower()).strip('-')[:48] or 'work'
        target = self.exposed / (slug + '--' + identity['edition'])
        if target.exists() or target.is_symlink():
            raise LibraryError('Transfer target already exists')
        self.capacity.check(self.root, max(state['assets'][asset_id]['size'] * 2, 2 * 1024**3))
        import tempfile
        with tempfile.TemporaryDirectory(prefix='resolution-preflight-', dir=self.staging) as folder:
            transform_archive(source, Path(folder), comicinfo(state, asset_id), asset_id,
                              check_capacity=lambda: self.capacity.check(self.root, 0))

    def load_operation(self, operation_id: str) -> dict[str, Any]:
        if not re.fullmatch('[0-9a-f]{32}', operation_id):
            raise LibraryError('Invalid operation ID')
        value: dict[str, Any] = yaml.safe_load((self.journals / (operation_id + '.yaml')).read_text())
        if value.get('id') != operation_id or value.get('schema') != 1:
            raise LibraryError('Invalid operation journal')
        if Path(value['target']).name != value['target']:
            raise LibraryError('Unsafe journal target')
        return value

    def commit(self, operation: dict[str, Any]) -> None:
        target = self.exposed / operation['target']
        if operation['kind'] == 'withdraw':
            if target.exists():
                raise LibraryError('Withdrawn target unexpectedly exists')
            verify(self.recovery / operation['id'], operation['manifest'])
        else:
            verify(target, operation['manifest'])
        state = self.store.read()
        edition = state['editions'][operation['edition']]
        current = edition.get('publication')
        if state['operations'].get(operation['id'], {}).get('status') == 'committed':
            self.transition(operation, 'committed')
            return
        if current and current.get('operation') == operation['id']:
            self.transition(operation, 'committed')
            return
        if state['revision'] != operation['catalogue_revision']:
            raise LibraryError('Published folder needs reconciliation after catalogue change')
        if operation['kind'] == 'withdraw':
            edition.pop('publication', None)
        else:
            artifact_hash = operation['manifest']['book.' + operation['artifact_format']]
            existing_artifact = state['artifacts'].get(artifact_hash)
            if existing_artifact and operation['edition'] not in existing_artifact['editions'] and not self.artifact_transfer_allowed(state, operation['asset'], operation.get('decision')):
                raise LibraryError('Generated artifact has conflicting edition ownership')
            artifact = state['artifacts'].setdefault(artifact_hash, {'sha256': artifact_hash,
                'source_assets': [], 'editions': [operation['edition']],
                'transformation': operation.get('transformation')})
            if operation['edition'] not in artifact['editions']:
                artifact['editions'].append(operation['edition'])
            if operation['asset'] not in artifact['source_assets']:
                artifact['source_assets'].append(operation['asset'])
            edition['publication'] = {'target': operation['target'], 'manifest': operation['manifest'],
                'operation': operation['id'], 'asset': operation['asset'], 'version': state['revision'] + 1,
                'asset_hash': operation['asset'], 'copy_hash': operation['manifest']['book.' + operation['artifact_format']],
                'artifact_format': operation['artifact_format'], 'export_version': operation.get('export_version'),
                'transformation': operation.get('transformation')}
        state['operations'][operation['id']] = {'kind': operation['kind'], 'edition': operation['edition'],
                                               'status': 'committed', 'journal': operation['id'] + '.yaml',
                                               'decision': operation.get('decision'), 'role': operation.get('role')}
        self.store.save(state, expected=state['revision'])
        self.transition(operation, 'committed')
        if operation['kind'] != 'withdraw':
            from library.provisional import retire_provisional
            retire_provisional(self.store, self.root, operation['asset'])

    def withdraw(self, edition_id: str, *, expected: int, decision: str | None = None, role: str | None = None) -> dict[str, Any]:
        with lock(self.store.directory / 'library-copy.lock', blocking=False):
            self.reconcile(locked=True)
            state = self.store.read()
            if state['revision'] != expected:
                raise LibraryError('Stale withdrawal version')
            previous = state['editions'][edition_id].get('publication')
            if not previous:
                raise LibraryError('Edition has no managed publication')
            self.decision_gate(state, previous['asset'], decision, role)
            target = self.exposed / previous['target']
            verify(target, previous['manifest'])
            private_tree(self.root)
            operation = {'schema': 1, 'id': uuid.uuid4().hex, 'kind': 'withdraw',
                'asset': previous['asset'], 'edition': edition_id, 'target': previous['target'],
                'artifact_format': previous['artifact_format'],
                'manifest': previous['manifest'], 'previous': previous,
                'catalogue_revision': expected, 'created_at': now(), 'decision': decision, 'role': role}
            self.transition(operation, 'intent')
            self.fault('before_substitution', operation)
            verify(target, previous['manifest'])
            rename_atomic(target, self.recovery / operation['id'])
            self.fault('after_substitution', operation)
            self.transition(operation, 'published')
            self.commit(operation)
            return operation

    def restore(self, operation_id: str, *, expected: int, decision: str | None = None, role: str | None = None) -> dict[str, Any]:
        with lock(self.store.directory / 'library-copy.lock', blocking=False):
            self.reconcile(locked=True)
            old = self.load_operation(operation_id)
            previous = old.get('previous')
            if not previous or old['phase'] != 'committed':
                raise LibraryError('Operation has no committed recovery revision')
            source = self.recovery / operation_id
            verify(source, previous['manifest'])
            state = self.store.read()
            if state['revision'] != expected:
                raise LibraryError('Stale recovery version')
            self.decision_gate(state, previous['asset'], decision, role)
            edition = state['editions'][old['edition']]
            selected = state['variants'].get(edition.get('selected'), {})
            if selected.get('asset') != previous['asset']:
                raise LibraryError('Select the recovery asset through an operator decision first')
            self.resolve_origin(state, previous['asset'])
            current = edition.get('publication')
            target = self.exposed / old['target']
            if current:
                verify(target, current['manifest'])
            elif target.exists():
                raise LibraryError('Recovery destination is unmanaged')
            private_tree(self.root)
            operation = {'schema': 1, 'id': uuid.uuid4().hex, 'kind': 'restore',
                'asset': previous['asset'], 'edition': old['edition'], 'target': old['target'],
                'artifact_format': previous['artifact_format'], 'export_version': previous.get('export_version'),
                'transformation': previous.get('transformation'),
                'manifest': previous['manifest'], 'previous': current,
                'catalogue_revision': expected, 'created_at': now(), 'restore_source': operation_id, 'decision': decision, 'role': role}
            self.transition(operation, 'intent')
            self.transition(operation, 'verified')
            self.fault('before_substitution', operation)
            verify(source, previous['manifest'])
            if current:
                verify(target, current['manifest'])
            rename_atomic(source, target, exchange=bool(current))
            self.fault('after_substitution', operation)
            if current:
                verify(source, current['manifest'])
                rename_atomic(source, self.recovery / operation['id'])
            self.transition(operation, 'published')
            self.commit(operation)
            return operation

    def reconcile(self, *, locked: bool = False, provisional: bool = True) -> list[dict[str, Any]]:
        if not locked:
            with lock(self.store.directory / 'library-copy.lock', blocking=False):
                return self.reconcile(locked=True, provisional=provisional)
        results = []
        if provisional:
            # An admission can have moved a committed legacy folder to recovery
            # before its catalogue save. Finish that journal before checking the
            # preceding edition exposure. Admission's own preflight passes False.
            from library.admission import AdmissionPublisher
            AdmissionPublisher(self.store, self.reception, self.root, exclusive=True,
                               capacity=self.capacity).reconcile(locked=True)
        for operation_id in self.store.read()['operations']:
            if not (self.journals / (operation_id + '.yaml')).exists():
                raise LibraryError('Catalogue operation journal is missing')
        operations = [self.load_operation(path.stem) for path in self.journals.glob('*.yaml')]
        # Complete the unfinished substitution before checking the preceding
        # committed revision, which may have legitimately moved to recovery.
        for operation in sorted(operations, key=lambda op: (op['phase'] == 'committed', op['created_at'])):
            phase = operation['phase']
            if phase in ('interrupted', 'blocked', 'capacity_aborted'):
                if phase == 'blocked':
                    raise LibraryError('Blocked operation requires explicit recovery: ' + operation['id'])
                continue
            target, stage = self.exposed / operation['target'], self.staging / operation['id']
            if phase == 'committed':
                # Superseded revisions are checked in recovery, not at current exposure.
                state = self.store.read()
                current = state['editions'][operation['edition']].get('publication', {})
                if current.get('operation') == operation['id']:
                    verify(target, operation['manifest'])
                continue
            if operation['kind'] == 'withdraw':
                recovery = self.recovery / operation['id']
                if not target.exists() and recovery.exists():
                    verify(recovery, operation['manifest'])
                    self.transition(operation, 'published')
                    self.commit(operation)
                elif target.exists() and not recovery.exists():
                    verify(target, operation['manifest'])
                    self.transition(operation, 'interrupted')
                else:
                    raise LibraryError('Unexplained withdrawal divergence')
                results.append(operation)
                continue
            if operation.get('restore_source'):
                stage = self.recovery / operation['restore_source']
            if target.exists() and manifest(target) == operation['manifest']:
                previous = operation['previous']
                if previous and stage.exists():
                    verify(stage, previous['manifest'])
                    rename_atomic(stage, self.recovery / operation['id'])
                self.transition(operation, 'published')
                self.commit(operation)
            elif phase in ('intent', 'transforming', 'transformed', 'prepared', 'verified'):
                if operation['previous']:
                    verify(target, operation['previous']['manifest'])
                elif target.exists():
                    raise LibraryError('Unknown destination after interrupted creation')
                # Preserve operation-owned partials as evidence. A later retry
                # gets a new operation ID and never reuses unverified bytes.
                self.transition(operation, 'interrupted')
            else:
                raise LibraryError('Unexplained publication divergence; preserve all evidence')
            results.append(operation)
        if provisional:
            from library.provisional import ProvisionalPublisher, retire_provisional
            for edition in self.store.read()['editions'].values():
                if edition.get('publication'):
                    verify(self.root / 'bd' / edition['publication']['target'], edition['publication']['manifest'])
                    retire_provisional(self.store, self.root, edition['publication']['asset'])
            ProvisionalPublisher(self.store, self.reception, self.root, exclusive=True, capacity=self.capacity).reconcile(locked=True)
        return results
