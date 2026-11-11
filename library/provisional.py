"""Technically validated copies with an explicitly unknown bibliographic edition."""
from __future__ import annotations

import hashlib
import html
import json
import re
import uuid
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from library.catalog import Catalog
from library.primitives import LibraryError, now, sync_dir
from library.publication import Publisher, Capacity, manifest, private_tree, publication_lock, rename_atomic, verify
from library.readers import fingerprint, sha256
from library.transform import transform_archive
from library.xmlmetadata import validate_xml


def eligible(asset: dict[str, Any]) -> bool:
    # Decisions and known editions retain their existing publication gates.
    return bool(asset.get('status') == 'validated' and not asset.get('identity')
                and not asset.get('resolution') and not asset.get('contested')
                and asset.get('disposition') not in ('exclude', 'defer')
                and not asset.get('provisional'))


def provisional_xml(asset_id: str, asset: dict[str, Any]) -> bytes:
    root = ET.Element('ComicInfo')
    # Embedded values are assertions, not validated editions. Do not repeat them
    # as authoritative Publisher/Writer/Number tags or infer from bundle labels.
    ET.SubElement(root, 'Notes').text = ('Booksys provisional collection; edition=unknown; '
        'identity=unconfirmed; source_asset_sha256=' + asset_id)
    if asset.get('pages'):
        ET.SubElement(root, 'PageCount').text = str(asset['pages'])
    data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    validate_xml(data)
    return bytes(data)


class ProvisionalPublisher(Publisher):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        from library.operational_state import CopyOperations
        self.operations=CopyOperations(self.store.backend()._database,'provisional')
        self.exposed = self.root / 'bd-provisional'
        self.journals = self.store.directory / 'library-provisional-operations'
        for path in (self.exposed, self.journals):
            if path.is_symlink():
                raise LibraryError('Symlink in provisional layout')
            path.mkdir(mode=0o700, exist_ok=True)
        if self.exposed.stat().st_dev != self.staging.stat().st_dev:
            raise LibraryError('Provisional staging must share its exposure filesystem')
        private_tree(self.root)

    def commit(self, operation: dict[str, Any]) -> None:
        verify(self.exposed / operation['target'], operation['manifest'])
        state = self.store.working_set('admit', {'assets': [operation['asset']]})
        asset = state['assets'][operation['asset']]
        previous = asset.get('provisional')
        if previous:
            if previous['operation'] != operation['id']:
                raise LibraryError('Provisional operation ownership conflict')
        else:
            if not eligible(asset):
                raise LibraryError('Provisional eligibility changed; preserve journal and copy')
            asset['provisional'] = {'operation': operation['id'], 'target': operation['target'],
                'manifest': operation['manifest'], 'artifact_format': 'cbz', 'edition_status': 'unknown',
                'status': 'published', 'transformation': operation['transformation']}
            self._terminal(operation,self.store.prepare(state))
            return
        self.transition(operation,'committed')

    def reconcile(self, *, locked: bool = False, provisional: bool = True, asset: str | None = None) -> list[dict[str, Any]]:
        if not locked:
            with publication_lock(self.store.directory, self.root):
                return self.reconcile(locked=True, asset=asset)
        operations = self.operations.records()
        for operation in operations:
            if (operation.get('kind') != 'provisional' or operation.get('target') != operation.get('asset')
                    or not re.fullmatch('[0-9a-f]{64}', str(operation.get('asset', '')))
                    or set(operation.get('manifest', {})) != {'book.cbz', 'fiche.md'}):
                raise LibraryError('Invalid provisional operation journal')
            if asset is not None and not self.operation_applies(operation, asset):
                continue
            target = self.exposed / operation['target']
            current = (self.store.get('assets', operation['asset']) or {}).get('provisional')
            if current and current['operation'] != operation['id']:
                raise LibraryError('Provisional journal ownership conflict')
            if current and current['status'] == 'recovered':
                verify(self.recovery / current['recovery'], current['manifest'])
                continue
            if operation['phase'] == 'committed':
                verify(target, operation['manifest'])
            elif target.exists():
                verify(target, operation['manifest'])
                self.commit(operation)
            elif operation['phase'] in ('prepared', 'verified'):
                stage = self.staging / operation['id']
                verify(stage, operation['manifest'])
                if not eligible((self.store.get('assets', operation['asset']) or {})):
                    raise LibraryError('Interrupted provisional copy eligibility changed')
                private_tree(self.root)
                self.capacity.check(self.root, 0)
                rename_atomic(stage, target, private_root=self.root)
                self.commit(operation)
            else:
                # A failed conversion is evidence. Never silently repeat it or
                # create another journal for this asset on the next cycle.
                raise LibraryError('Interrupted provisional copy requires recovery: ' + operation['id'])
        for key, entry in self.store.records('assets'):
            if asset is not None and key != asset:
                continue
            record = entry.get('provisional')
            if record and not any(op['id'] == record['operation'] for op in operations):
                raise LibraryError('Provisional catalogue journal is missing')
        return operations

    def publish_unknown(self, asset_id: str) -> dict[str, Any]:
        with publication_lock(self.store.directory, self.root):
            # Reconcile both namespaces before starting a new conversion.
            Publisher(self.store, self.reception, self.root, exclusive=True, capacity=self.capacity).reconcile(locked=True, provisional=False)
            self.reconcile(locked=True)
            state = self.store.working_set('admit', {'assets': [asset_id]})
            asset = state['assets'][asset_id]
            if not eligible(asset):
                raise LibraryError('Asset is not eligible for provisional copying')
            source = self.resolve_origin(state, asset_id)
            target = self.exposed / asset_id
            if target.exists() or target.is_symlink():
                raise LibraryError('Unmanaged provisional target')
            fiche = ('# Provisional comic copy\n\nEdition: unknown\nIdentity: unconfirmed\n'
                'Source asset SHA-256: ' + asset_id + '\n\n## Untrusted source assertions\n\n<pre>' +
                html.escape(json.dumps({'embedded': asset.get('embedded', {}),
                    'assertions': asset.get('assertions', []),
                    'origins': [state['origins'][key] for key in asset['origins']]},
                    ensure_ascii=False, indent=2)) + '</pre>\n').encode()
            required = (2 * 1024**3 if asset['format'] == 'pdf' else
                        sum(page['size'] for page in asset.get('page_manifest', []))) + len(fiche) + 8 * 1024**2
            self.capacity.check(self.root, required)
            operation: dict[str, Any] = {'schema': 1, 'id': uuid.uuid4().hex, 'kind': 'provisional',
                'asset': asset_id, 'target': asset_id, 'manifest': {'book.cbz': '',
                'fiche.md': hashlib.sha256(fiche).hexdigest()}, 'created_at': now()}
            self.transition(operation, 'intent')
            stage = self.staging / operation['id']
            transformed = self.staging / (operation['id'] + '-transform')
            stage.mkdir(mode=0o700); transformed.mkdir(mode=0o700)
            before = fingerprint(source)
            self.transition(operation, 'transforming')
            result = transform_archive(source, transformed, provisional_xml(asset_id, asset), asset_id,
                check_capacity=lambda: self.check_capacity(operation, len(fiche)))
            if ((asset['format'] != 'pdf' and result['page_manifest'] != asset.get('page_manifest')) or
                    (asset['format'] == 'pdf' and result['pages'] != asset['pages'])):
                raise LibraryError('Provisional conversion page readback differs from source')
            if fingerprint(source) != before or sha256(source) != asset_id:
                raise LibraryError('Provisional source changed')
            operation['transformation'] = result
            rename_atomic(transformed / 'book.cbz', stage / 'book.cbz')
            transformed.rmdir()
            operation['manifest']['book.cbz'] = sha256(stage / 'book.cbz')
            with (stage / 'fiche.md').open('xb') as output:
                import os
                os.fchmod(output.fileno(), 0o600)
                output.write(fiche); output.flush(); os.fsync(output.fileno())
            sync_dir(stage)
            self.transition(operation, 'prepared')
            verify(stage, operation['manifest'])
            self.capacity.check(self.root, 0)
            if self.store.revision != state['revision']:
                raise LibraryError('Catalogue changed during provisional conversion')
            private_tree(self.root)
            self.transition(operation, 'verified')
            rename_atomic(stage, target, private_root=self.root)
            self.fault('after_substitution', operation)
            self.transition(operation, 'published')
            self.commit(operation)
            return operation




def process_provisional(store: Catalog, reception: Path, library: Path, *, limit: int = 1,
                        isolated: bool = False, lab: bool = True,
                        capacity: Capacity | None = None) -> dict[str, int]:
    if limit <= 0:
        backlog = sum(eligible(asset) for asset in store.read()['assets'].values())
        return {'processed': 0, 'published': 0, 'backlog': backlog, 'ready_backlog': backlog}
    publisher = ProvisionalPublisher(store, reception, library, isolated=isolated, lab=lab, capacity=capacity)
    publisher.reconcile()
    processed = 0
    for key, asset in store.read()['assets'].items():
        if processed >= limit:
            break
        if eligible(asset):
            publisher.publish_unknown(key)
            processed += 1
    backlog = sum(eligible(asset) for asset in store.read()['assets'].values())
    return {'processed': processed, 'published': processed, 'backlog': backlog, 'ready_backlog': backlog}


from library.publication import retire_provisional as retire_provisional
