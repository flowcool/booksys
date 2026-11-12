"""Journaled independent copies. Linux atomic directory publication only."""
from __future__ import annotations

import base64
import ctypes
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
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

from library.media_paths import writer_uid
from library.catalog import Catalog
from library.primitives import LibraryError, atomic_yaml, digest, lock, now, sync_dir
from library.reception import reception_path
from library.readers import fingerprint, sha256
from library.transform import transform_archive, VERSION
from library.xmlmetadata import comicinfo


_copy_root: ContextVar[Path | None] = ContextVar('publication_copy_root', default=None)


@contextmanager
def publication_lock(directory: Path, root: Path) -> Iterator[None]:
    """Bind the already required copy lock to its private publication tree."""
    with lock(directory/'library-copy.lock', blocking=False):
        token = _copy_root.set(root.absolute())
        try:
            yield
        finally:
            _copy_root.reset(token)


def rename_atomic(source: Path, target: Path, *, exchange: bool = False,
                  private_root: Path | None = None) -> None:
    """Publish without replacement; callers hold the shared library-copy lock.

    NFSv3 rejects renameat2 flags. Files can use an exclusive hard link. A new
    directory can use POSIX rename only inside the explicitly private, single
    writer tree. Existing-directory exchange still requires kernel support.
    """
    import errno
    from library.locking import check_effect
    check_effect()
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'renameat2', None)
    if function is None:
        raise LibraryError('renameat2 is required; no unsafe rename fallback')
    result = function(-100, os.fsencode(source), -100, os.fsencode(target), 2 if exchange else 1)
    if result:
        code = ctypes.get_errno()
        if exchange or code not in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
            raise LibraryError(f'Atomic directory rename failed: {os.strerror(code)}')
        try:
            if source.is_symlink():
                raise LibraryError('Symlink in atomic publication')
            if source.is_file():
                # link is an atomic no-replace operation even on NFSv3. Sync the
                # new name before releasing the old name; a crash retains bytes.
                os.link(source, target)
                sync_dir(target.parent)
                source.unlink()
            elif private_root is not None and source.is_dir():
                root = private_root.absolute()
                if _copy_root.get() != root:
                    raise LibraryError('Atomic directory publication requires the held library-copy lock')
                paths = (root, source.absolute(), target.absolute())
                if (any('..' in path.parts or path.resolve() != path for path in paths)
                        or not paths[1].is_relative_to(root) or not paths[2].is_relative_to(root)):
                    raise LibraryError('Atomic directory publication is outside the private library')
                private_tree(root)
                # No other authorized writer can create this name: the caller
                # owns library-copy.lock and the complete tree is owner-only.
                if target.exists() or target.is_symlink():
                    raise LibraryError('Atomic directory publication target already exists')
                check_effect()
                os.rename(source, target)
            else:
                raise LibraryError('Atomic directory publication requires exclusive private ownership')
        except OSError as exc:
            raise LibraryError(f'Atomic publication failed: {exc.strerror}') from exc
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
        if stat.S_ISLNK(info.st_mode) or info.st_uid != writer_uid(entry) or info.st_mode & 0o022:
            raise LibraryError('Exclusive library ownership is not established')


class Capacity:
    def __init__(self, *, reserve: int = 0,
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
            raise LibraryError('Insufficient capacity for remaining copy and configured reserve')


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
                 exclusive: bool = False, local_recovery: bool = False, prepared: list[Any] | None = None):
        self.store, self.reception, self.root = store, reception.resolve(), root.resolve()
        self.isolated, self.capacity, self.fault = isolated, capacity or Capacity(), fault
        self.local_recovery = local_recovery
        self.prepared=prepared
        self.completion_task: Any=None
        if local_recovery and store.backend() is None:
            raise LibraryError('Local recovery requires scoped PostgreSQL authority')
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
        from library.operational_state import CopyOperations
        self.operations=CopyOperations(store.backend()._database,'publication')
        for directory in (self.root, self.exposed, self.staging, self.recovery, self.journals):
            if directory.is_symlink():
                raise LibraryError('Symlink in library layout')
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if len({d.stat().st_dev for d in (self.exposed, self.staging, self.recovery)}) != 1:
            raise LibraryError('Library staging and recovery must share the exposure filesystem')
        private_tree(self.root)

    def _phase_evidence(self, operation: dict[str,Any]) -> None:
        from library.codec import encode
        import hashlib
        evidence=self.journals/'evidence'/operation['id']/(hashlib.sha256(encode(operation).encode()).hexdigest()+'.yaml')
        if not evidence.exists():
            atomic_yaml(evidence,operation,exclusive=True)

    def _terminal(self, operation: dict[str,Any], result: Any) -> None:
        from dataclasses import replace
        from library.postgres import CommitUnknown
        operation['phase']='committed'
        self._phase_evidence(operation)
        current=self.operations.get(operation['id'])
        if current is not None and current.get('phase')=='committed':
            self.fault('committed',operation)
            return
        result=replace(result,completions=[*result.completions,self.operations.prepare(dict(operation))])
        if self.prepared is not None:
            self.prepared.append(result)
        else:
            backend=self.store.backend()
            request=dict(expected_revision=result.revision,conflict=result.conflict,completions=result.completions)
            identity=backend.transition_digest(result.change,**request)
            key='copy:'+self.operations.namespace+':'+operation['id']+':committed'
            try:
                backend.commit(result.change,**request,commit_key=key)
            except CommitUnknown:
                if backend.outcome(key,input_digest=identity)['state']!='committed':
                    raise
        self.fault('committed',operation)

    def transition(self, operation: dict[str, Any], phase: str) -> None:
        if phase=='committed':
            from library.store import PreparedResult,Change
            self._terminal(operation,PreparedResult(Change(),None,0))
            return
        operation['phase']=phase
        self._phase_evidence(operation)
        self.operations.phase(operation)
        self.fault(phase,operation)

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
        with publication_lock(self.store.directory, self.root):
            self.reconcile(locked=True, asset=asset_id if self.local_recovery else None)
            self.store.project_pending()
            state = self.store.result_set('publish', {'assets': [asset_id]})
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
                'conflict': getattr(state, 'conflict', None),
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
            existing_artifact = self.store.get('artifacts', expected[book_name])
            if existing_artifact and identity['edition'] not in existing_artifact['editions'] and not self.artifact_transfer_allowed(state, asset_id, decision):
                raise LibraryError('Generated artifact has conflicting edition ownership')
            self.fault('before_substitution', operation)
            if operation.get('conflict'):
                operation['conflict'] = self.store.extend_result(operation['conflict'],
                    {'artifacts': [expected[book_name]], 'operations': [operation['id']]})
                self.transition(operation, 'verified')
            elif self.store.revision != operation['catalogue_revision']:
                raise LibraryError('Catalogue changed while preparing publication')
            if previous:
                verify(target, previous['manifest'])
            private_tree(self.root)
            rename_atomic(stage, target, exchange=bool(previous), private_root=self.root)
            self.fault('after_substitution', operation)
            if previous:
                # A dedicated writer is a prerequisite. This second check also
                # detects a raced external edit; it does not make rename a CAS.
                try:
                    verify(stage, previous['manifest'])
                except LibraryError:
                    verify(target, expected)
                    rename_atomic(stage, target, exchange=True, private_root=self.root)
                    self.transition(operation, 'blocked')
                    raise
                rename_atomic(stage, self.recovery / operation['id'], private_root=self.root)
            self.transition(operation, 'published')
            self.commit(operation)
            return operation

    def artifact_transfer_allowed(self, state: dict[str, Any], asset: str, decision: str | None) -> bool:
        return bool(decision and any(op.get('decision') == decision and op.get('role') == 'withdraw'
                    and op['status'] == 'committed' for op in state['operations'].values())
                    and not any(e.get('publication', {}).get('asset') == asset for e in state['editions'].values()))

    def decision_gate(self, state: dict[str, Any], asset: str, decision: str | None, role: str | None) -> None:
        from library.decision_model import gate
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
        state = self.store.result_set('publish', {'assets': [asset_id]})
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
        value=self.operations.get(operation_id)
        if value is None:
            raise LibraryError('Copy operation authority unavailable')
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
        state = self.store.working_set('publish', {'editions': [operation['edition']],
            'assets': [operation['asset']], 'operations': [operation['id']],
            'artifacts': list(operation.get('manifest', {}).values())})
        edition = state['editions'][operation['edition']]
        current = edition.get('publication')
        if state['operations'].get(operation['id'], {}).get('status') == 'committed':
            self.transition(operation, 'committed')
            return
        if current and current.get('operation') == operation['id']:
            self.transition(operation, 'committed')
            return
        if operation.get('conflict'):
            if self.completion_task is not None:
                self.store.backend()._verify_completion_result(operation['conflict'],(self.completion_task.key,self.completion_task.generation))
            else:self.store.verify_result(operation['conflict'])
            from library.primitives import WorkingState
            if not isinstance(state, WorkingState):
                raise LibraryError('Scoped operation requires PostgreSQL working set')
            state.conflict = operation['conflict']
        elif state['revision'] != operation['catalogue_revision']:
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
        if operation['kind']!='withdraw':
            retire_provisional(self.store,self.root,operation['asset'],state=state)
        self._terminal(operation,self.store.prepare(state))

    def withdraw(self, edition_id: str, *, expected: int, decision: str | None = None, role: str | None = None) -> dict[str, Any]:
        with publication_lock(self.store.directory, self.root):
            self.reconcile(locked=True)
            state = self.store.result_set('publish', {'editions': [edition_id]})
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
                'catalogue_revision': expected, 'conflict': getattr(state, 'conflict', None), 'created_at': now(), 'decision': decision, 'role': role}
            self.transition(operation, 'intent')
            self.fault('before_substitution', operation)
            if operation.get('conflict'):
                self.store.verify_result(operation['conflict'])
            verify(target, previous['manifest'])
            rename_atomic(target, self.recovery / operation['id'], private_root=self.root)
            self.fault('after_substitution', operation)
            self.transition(operation, 'published')
            self.commit(operation)
            return operation

    def restore(self, operation_id: str, *, expected: int, decision: str | None = None, role: str | None = None) -> dict[str, Any]:
        with publication_lock(self.store.directory, self.root):
            self.reconcile(locked=True)
            old = self.load_operation(operation_id)
            previous = old.get('previous')
            if not previous or old['phase'] != 'committed':
                raise LibraryError('Operation has no committed recovery revision')
            source = self.recovery / operation_id
            verify(source, previous['manifest'])
            state = self.store.result_set('publish', {'editions': [old['edition']], 'assets': [previous['asset']]})
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
                'catalogue_revision': expected, 'conflict': getattr(state, 'conflict', None), 'created_at': now(), 'restore_source': operation_id, 'decision': decision, 'role': role}
            self.transition(operation, 'intent')
            self.transition(operation, 'verified')
            self.fault('before_substitution', operation)
            if operation.get('conflict'):
                self.store.verify_result(operation['conflict'])
            verify(source, previous['manifest'])
            if current:
                verify(target, current['manifest'])
            rename_atomic(source, target, exchange=bool(current), private_root=self.root)
            self.fault('after_substitution', operation)
            if current:
                verify(source, current['manifest'])
                rename_atomic(source, self.recovery / operation['id'], private_root=self.root)
            self.transition(operation, 'published')
            self.commit(operation)
            return operation

    def operation_applies(self, operation: dict[str, Any], asset: str) -> bool:
        """Validate ownership even when skipping a known unrelated operation."""
        if not re.fullmatch('[0-9a-f]{64}', str(operation.get('asset', ''))):
            raise LibraryError('Copy journal has unknown conflict scope')
        target = str(operation.get('target', ''))
        if operation.get('kind') == 'admission':
            # Each admission leaf owns an immutable asset hash and signature.
            if not re.search('--' + operation['asset'] + '--[0-9a-f]{12}$', target):
                raise LibraryError('Copy journal has unknown destination scope')
        elif operation.get('kind') == 'provisional':
            if operation['asset'] not in target:
                raise LibraryError('Copy journal has unknown destination scope')
        elif not operation.get('edition') or not target.endswith('--' + str(operation['edition'])):
            raise LibraryError('Copy journal has unknown destination scope')
        if operation['asset'] == asset:
            return True
        if not operation.get('edition'):
            return False
        state = self.store.working_set('admit', {'assets': [asset]}, media=False)
        identity = state['assets'].get(asset, {}).get('identity', {})
        return bool(operation.get('edition') and operation['edition'] == identity.get('edition'))

    def reconcile(self, *, locked: bool = False, provisional: bool = True, asset: str | None = None) -> list[dict[str, Any]]:
        if not locked:
            with publication_lock(self.store.directory, self.root):
                return self.reconcile(locked=True, provisional=provisional, asset=asset)
        results = []
        for operation_id, record in self.store.records('operations'):
            if asset is not None and record.get('edition') and record.get('edition') not in self.store.working_set('admit', {'assets': [asset]}, media=False)['editions']:
                continue
            if self.operations.get(operation_id) is None:
                raise LibraryError('Catalogue operation journal is missing')
        operations = self.operations.records()
        if asset is not None:
            operations = [op for op in operations if self.operation_applies(op, asset)]
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
                current = (self.store.get('editions', operation['edition']) or {}).get('publication', {})
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
                    rename_atomic(stage, self.recovery / operation['id'], private_root=self.root)
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
        return results


def retire_provisional(store: Catalog, root: Path, asset_id: str, *, state: dict[str,Any] | None = None) -> None:
    """Called under the shared copy lock after confirmed commit; retain all bytes."""
    detached=state is not None
    state = state if state is not None else store.working_set('admit', {'assets': [asset_id]})
    record = state['assets'][asset_id].get('provisional')
    if not record or record['status'] == 'recovered':
        return
    source = root / 'bd-provisional' / record['target']
    name = 'provisional-' + record['operation']
    recovery = root / '.recovery' / name
    if source.exists() and not recovery.exists():
        verify(source, record['manifest'])
        rename_atomic(source, recovery, private_root=root)
    elif source.exists() or not recovery.exists():
        raise LibraryError('Unexplained provisional retirement divergence')
    verify(recovery, record['manifest'])
    record.update(status='recovered', recovery=name)
    if not detached:
        store.save(state,expected=state['revision'])
