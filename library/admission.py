"""Journaled work-level CBZ admission, independent of exact edition selection."""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid

import yaml

from library.catalog import LibraryError, digest, lock, now, sync_dir
from library.publish import Publisher, private_tree, rename_atomic, verify
from library.readers import sha256
from library.transform import transform_archive
from library.xmlmetadata import work_comicinfo


def work_fields(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    asset = state['assets'][key]
    result = asset.get('work_result', {})
    if result.get('status') == 'identified':
        return {**result['work'], 'field_conflicts': result.get('field_conflicts', {})}
    identity = asset.get('identity')
    if identity and not asset.get('contested'):
        return {**state['works'][identity['work']], **state['editions'][identity['edition']]}
    return None


def eligible(state: dict[str, Any], key: str) -> bool:
    asset = state['assets'][key]
    # Prepared legacy decisions retain exclusive control of their application.
    return bool(asset.get('status') == 'validated'
                and asset.get('resolution', {}).get('status') not in ('pending', 'blocked')
                and not asset.get('contested') and asset.get('disposition') not in ('exclude', 'defer'))


def retire_legacy(state: dict[str, Any], key: str, root: Path) -> bool:
    """Retain exact-edition exposure in recovery after verified CBZ admission.

    Caller holds the copy lock. The deterministic recovery name also makes a
    crash after rename and before catalogue save safe to reconcile.
    """
    changed = False
    for edition in state['editions'].values():
        record = edition.get('publication', {})
        if record.get('asset') != key:
            continue
        if Path(record['target']).name != record['target']:
            raise LibraryError('Unsafe legacy publication target')
        source = root/'bd'/record['target']
        recovery = root/'.recovery'/('edition-'+record['operation'])
        if source.exists() and not recovery.exists():
            verify(source, record['manifest'])
            rename_atomic(source, recovery)
        elif source.exists() or not recovery.exists():
            raise LibraryError('Legacy admission retirement divergence')
        verify(recovery, record['manifest'])
        state['assets'][key].setdefault('edition_recovery', []).append({**record, 'recovery': recovery.name})
        edition.pop('publication')
        changed = True
    return changed


def slug(value: Any) -> str:
    text = re.sub(r'[^\w .-]+', '-', str(value), flags=re.UNICODE).strip(' .-')[:80]
    return text if text and text not in ('.', '..') else 'unknown'


def projection(state: dict[str, Any], key: str) -> dict[str, Any]:
    asset = state['assets'][key]
    work = work_fields(state, key)
    fields = {**(work or {}), **asset.get('overrides', {})}
    status = 'classified' if work else 'holding'
    signature = digest({'asset': key, 'work': work, 'overrides': asset.get('overrides', {})})
    leaf = slug((str(fields.get('number', '')).zfill(4) + ' - ' if fields.get('number') else '') +
                str(fields.get('title') or 'À identifier')) + '--' + key + '--' + signature[:12]
    family = str(fields.get('family')) if fields.get('family') in ('BD', 'Manga', 'Comics') else 'Unknown family'
    target = str(Path('classified') / family / slug(fields.get('series') or fields.get('title')) / leaf) if work else str(Path('holding') / leaf)
    return {'status': status, 'signature': signature, 'target': target, 'fields': work or {}}


class AdmissionPublisher(Publisher):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.journals = self.store.directory / 'library-admission-operations'
        if self.journals.is_symlink():
            raise LibraryError('Symlink in admission layout')
        self.journals.mkdir(mode=0o700, exist_ok=True)

    def target_path(self, relative: str) -> Path:
        path = Path(relative)
        if (path.is_absolute() or '..' in path.parts or not path.parts
                or path.parts[0] not in ('holding', 'classified')):
            raise LibraryError('Unsafe admission path')
        target = self.root / path
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise LibraryError('Symlink in admission path')
        return target

    def prepare_parent(self, target: Path) -> None:
        for length in range(1, len(target.parent.relative_to(self.root).parts) + 1):
            (self.root.joinpath(*target.parent.relative_to(self.root).parts[:length])).mkdir(mode=0o700, exist_ok=True)

    def load_operation(self, operation_id: str) -> dict[str, Any]:
        if not re.fullmatch('[0-9a-f]{32}', operation_id):
            raise LibraryError('Invalid admission operation ID')
        value: dict[str, Any] = yaml.safe_load((self.journals/(operation_id+'.yaml')).read_text())
        if (value.get('schema') != 1 or value.get('id') != operation_id or value.get('kind') != 'admission'
                or not re.fullmatch('[0-9a-f]{64}', str(value.get('asset', '')))
                or set(value.get('manifest', {})) != {'book.cbz', 'fiche.md'}):
            raise LibraryError('Invalid admission journal')
        self.target_path(value['target'])
        return value

    def commit(self, operation: dict[str, Any]) -> None:
        target = self.target_path(operation['target'])
        verify(target, operation['manifest'])
        state = self.store.read()
        current = state['assets'][operation['asset']].get('admission')
        if current and current['operation'] == operation['id']:
            self.transition(operation, 'committed')
            return
        if state['revision'] != operation['catalogue_revision']:
            raise LibraryError('Admission catalogue changed; preserve journal for reconciliation')
        if current != operation['previous']:
            raise LibraryError('Admission ownership changed')
        if current:
            previous = self.target_path(current['target'])
            recovery = self.recovery / operation['id']
            if previous.exists() and not recovery.exists():
                verify(previous, current['manifest'])
                rename_atomic(previous, recovery)
            elif previous.exists() or not recovery.exists():
                raise LibraryError('Admission recovery divergence')
            verify(recovery, current['manifest'])
        state['assets'][operation['asset']]['admission'] = {
            key: operation[key] for key in ('target', 'manifest', 'signature', 'status', 'transformation')}
        state['assets'][operation['asset']]['admission']['operation'] = operation['id']
        retire_legacy(state, operation['asset'], self.root)
        self.store.save(state, expected=state['revision'])
        self.transition(operation, 'committed')
        from library.provisional import retire_provisional
        retire_provisional(self.store, self.root, operation['asset'])

    def reconcile(self, *, locked: bool = False, provisional: bool = True) -> list[dict[str, Any]]:
        if not locked:
            with lock(self.store.directory/'library-copy.lock', blocking=False):
                return self.reconcile(locked=True)
        operations = [self.load_operation(p.stem) for p in sorted(self.journals.glob('*.yaml'))]
        for op in sorted(operations, key=lambda value: (value['phase'] == 'committed', value['created_at'])):
            target = self.target_path(op['target'])
            if op['phase'] == 'committed':
                current = self.store.read()['assets'][op['asset']].get('admission', {})
                if current.get('operation') == op['id']:
                    verify(target, op['manifest'])
                continue
            if not target.exists() and op['phase'] in ('prepared', 'verified'):
                stage = self.staging / op['id']
                verify(stage, op['manifest'])
                self.prepare_parent(target)
                private_tree(self.root)
                self.capacity.check(self.root, 0)
                rename_atomic(stage, target)
            if target.exists():
                self.commit(op)
            else:
                raise LibraryError('Interrupted admission requires recovery: ' + op['id'])
        for key, asset in self.store.read()['assets'].items():
            current = asset.get('admission')
            if current and not any(op['id'] == current['operation'] and op['asset'] == key for op in operations):
                raise LibraryError('Admission journal missing')
            if current and eligible(self.store.read(), key):
                verify(self.target_path(current['target']), current['manifest'])
                state = self.store.read()
                if retire_legacy(state, key, self.root):
                    self.store.save(state, expected=state['revision'])
                from library.provisional import retire_provisional
                retire_provisional(self.store, self.root, key)
        return operations

    def publish_asset(self, key: str) -> dict[str, Any]:
        with lock(self.store.directory/'library-copy.lock', blocking=False):
            Publisher(self.store, self.reception, self.root, exclusive=True,
                      capacity=self.capacity).reconcile(locked=True, provisional=False)
            self.reconcile(locked=True)
            state = self.store.read()
            if not eligible(state, key):
                raise LibraryError('Asset is not eligible for work admission')
            asset = state['assets'][key]
            desired = projection(state, key)
            current = asset.get('admission')
            if current and current['signature'] == desired['signature']:
                return self.load_operation(current['operation'])
            source = self.resolve_origin(state, key)
            target = self.target_path(desired['target'])
            if target.exists():
                raise LibraryError('Unmanaged admission target')
            evidence = {'asset': key, 'edition_status': 'confirmed' if asset.get('identity') else 'unknown',
                        'work': asset.get('work_result'), 'origins': [state['origins'][oid] for oid in asset['origins']]}
            fiche = ('# ' + ('À identifier' if desired['status'] == 'holding' else html.escape(str(desired['fields'].get('title')))) +
                     '\n\n<pre>' + html.escape(json.dumps(evidence, ensure_ascii=False, indent=2)) + '</pre>\n').encode()
            fields = desired['fields']
            notes = 'Booksys; source_asset_sha256=' + key + '; edition=' + evidence['edition_status']
            old_notes = asset.get('embedded', {}).get('Notes', '')
            xml = work_comicinfo(asset, {**fields, 'notes': str(old_notes) + '\n' + notes})
            required = (2*1024**3 if asset['format'] == 'pdf' else sum(p['size'] for p in asset.get('page_manifest', []))) + len(fiche) + 8*1024**2
            self.capacity.check(self.root, required)
            op: dict[str, Any] = {'schema': 1, 'id': uuid.uuid4().hex, 'kind': 'admission',
                'asset': key, **{k: desired[k] for k in ('target', 'signature', 'status')},
                'manifest': {'book.cbz': '', 'fiche.md': hashlib.sha256(fiche).hexdigest()},
                'catalogue_revision': state['revision'], 'previous': current, 'created_at': now()}
            self.transition(op, 'intent')
            stage = self.staging/op['id']
            converted = self.staging/(op['id']+'-transform')
            stage.mkdir(mode=0o700); converted.mkdir(mode=0o700)
            self.transition(op, 'transforming')
            result = transform_archive(source, converted, xml, key,
                check_capacity=lambda: self.check_capacity(op, len(fiche)))
            if ((asset['format'] != 'pdf' and result['page_manifest'] != asset.get('page_manifest'))
                    or result['pages'] != asset['pages'] or sha256(source) != key):
                raise LibraryError('Admission page readback or source hash changed')
            op['transformation'] = result
            rename_atomic(converted/'book.cbz', stage/'book.cbz')
            converted.rmdir()
            op['manifest']['book.cbz'] = sha256(stage/'book.cbz')
            with (stage/'fiche.md').open('xb') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(fiche); handle.flush(); os.fsync(handle.fileno())
            sync_dir(stage)
            self.transition(op, 'prepared')
            verify(stage, op['manifest'])
            if self.store.read()['revision'] != state['revision']:
                raise LibraryError('Catalogue changed during admission')
            self.prepare_parent(target)
            private_tree(self.root)
            self.capacity.check(self.root, 0)
            self.transition(op, 'verified')
            rename_atomic(stage, target)
            self.fault('after_substitution', op)
            self.transition(op, 'published')
            self.commit(op)
            return op


def process_admissions(store: Any, reception: Path, library: Path, *, limit: int = 1,
                       **options: Any) -> dict[str, int]:
    state = store.read()
    ready = [key for key in state['assets'] if eligible(state, key) and
             state['assets'][key].get('admission', {}).get('signature') != projection(state, key)['signature']]
    if limit > 0:
        options.setdefault('lab', True)
        writer = AdmissionPublisher(store, reception, library, **options)
        writer.reconcile()
        for key in ready[:limit]:
            writer.publish_asset(key)
    processed = min(len(ready), max(0, limit))
    return {'processed': processed, 'published': processed, 'backlog': len(ready)-processed,
            'ready_backlog': len(ready)-processed}
