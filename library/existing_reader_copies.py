"""Explicit selection and reversible retention of published reader copies."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
from typing import Any

import yaml

from library.admission import admission_path, eligible_asset, verify_admission
from library.catalog import Catalog
from library.operational_state import CopyOperations
from library.primitives import LibraryError, atomic_yaml, digest, sync_dir
from library.publication import private_tree, publication_lock, rename_atomic, verify
from library.reader_selection import identity, keeps_edition, output_pages, work_comparison, work_leads


class ExistingReaderCopies:
    def __init__(self, store: Catalog, root: Path):
        self.store, self.root = store, root.absolute()
        self.operations = CopyOperations(store.backend()._database, 'admission')
        self.recovery = self.root / '.recovery' / 'reader-selection'
        if not self.root.is_dir() or any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise LibraryError('Reader library is missing or has a symlink')

    def groups(self) -> dict[str, Any]:
        works = [(key, identity(summary)) for key, summary in self.store.backend().classified_works()]
        leads: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for index, (_, work) in enumerate(works):
            for lead in work_leads(work):
                leads[lead].append(index)
        records: dict[str, dict[str, Any]] = {}
        groups = []
        for index, (key, work) in enumerate(works):
            peers = {i for lead in work_leads(work) for i in leads[lead] if i != index}
            equal = []
            for i in sorted(peers):
                peer, other = works[i]
                if not work_comparison(work, other)[1] or not keeps_edition(other, work):
                    continue
                for asset in (key, peer):
                    if asset not in records:
                        record = self.store.backend().get('assets', asset, media=False) or {}
                        records[asset] = record
                first, second = (records[asset].get('admission', {}) for asset in (key, peer))
                pages = output_pages(key, first)
                if not pages or pages != output_pages(peer, second):
                    continue
                try:
                    self.verify_exposed(key, first)
                    self.verify_exposed(peer, second)
                except (LibraryError, OSError):
                    continue
                if not self.store.backend().default_in_use(peer):
                    equal.append({'asset': peer, 'target': second['target']})
            if equal:
                groups.append({'keep': key, 'target': records[key]['admission']['target'],
                               'equal_pages': len(output_pages(key, records[key]['admission'])),
                               'eligible_to_retain': equal})
        # Only keep choices that offer an additional selection over an earlier default.
        listed: list[set[str]] = []
        unique = []
        for group in groups:
            members = {group['keep'], *(row['asset'] for row in group['eligible_to_retain'])}
            if not any(members <= earlier for earlier in listed):
                unique.append(group)
                listed.append(members)
        return {'status': 'dry-run', 'groups': unique}

    def verify_exposed(self, key: str, admission: dict[str, Any]) -> None:
        if admission.get('status') != 'classified':
            raise LibraryError('Selection requires a classified copy')
        verify_admission(self.operations, key, admission)
        verify(admission_path(self.root, admission['target']), admission['manifest'])

    def journal(self, ident: str) -> Path:
        if not re.fullmatch('[0-9a-f]{64}', ident):
            raise LibraryError('Invalid selection journal ID')
        path = self.recovery / ident / 'selection.yaml'
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise LibraryError('Symlink in reader recovery path')
        return path

    def load(self, ident: str) -> dict[str, Any]:
        value = yaml.safe_load(self.journal(ident).read_text())
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('id') != ident
                or any(not re.fullmatch('[0-9a-f]{64}', str(value.get(k, ''))) for k in ('keep', 'hide'))
                or value['keep'] == value['hide']
                or ident != digest([value['keep'], value['hide'], value['kept_operation'], value['admission']['operation']])):
            raise LibraryError('Invalid reader selection journal')
        return value

    def pair(self, keep: str, hide: str, *, apply: bool = False) -> dict[str, Any]:
        if keep == hide or any(not re.fullmatch('[0-9a-f]{64}', key) for key in (keep, hide)):
            raise LibraryError('Explicit selection requires two distinct asset SHA-256 values')
        if apply:
            with publication_lock(self.store.directory, self.root):
                return self.select(keep, hide, apply=True)
        return self.select(keep, hide, apply=False)

    def select(self, keep: str, hide: str, *, apply: bool) -> dict[str, Any]:
        state = self.store.result_set('admit', {'assets': [keep, hide]})
        kept, extra = (state['assets'].get(key, {}) for key in (keep, hide))
        kept_admission = kept.get('admission', {})
        self.verify_exposed(keep, kept_admission)
        if self.store.backend().default_in_use(hide):
            raise LibraryError('Copy serves as the default for another asset')
        if any(not eligible_asset(row) for row in (kept, extra)):
            raise LibraryError('Both assets must be validated, eligible works')
        default, incoming = identity(kept), identity(extra)
        if not work_comparison(incoming, default)[1] or not keeps_edition(incoming, default):
            raise LibraryError('Work or edition does not permit selection')
        current = extra.get('admission')
        previous = extra.get('reader_selection', {})
        if current:
            ident = digest([keep, hide, kept_admission['operation'], current['operation']])
            plan = {'schema': 1, 'id': ident, 'keep': keep, 'hide': hide, 'admission': current,
                    'kept_operation': kept_admission['operation'], 'previous': previous}
        else:
            plan = self.load(previous.get('journal', ''))
            if (plan['keep'], plan['hide'], plan['kept_operation']) != (keep, hide, kept_admission['operation']):
                raise LibraryError('Reader selection differs from the retained journal')
        admission = plan['admission']
        pages = output_pages(hide, admission)
        if not pages or pages != output_pages(keep, kept_admission):
            raise LibraryError('Equal ordered output pages are required')
        return self.move(state, plan, restore=False, apply=apply)

    def restore(self, ident: str, *, apply: bool = False) -> dict[str, Any]:
        if apply:
            with publication_lock(self.store.directory, self.root):
                return self.restore_locked(ident, apply=True)
        return self.restore_locked(ident, apply=False)

    def restore_locked(self, ident: str, *, apply: bool) -> dict[str, Any]:
        plan = self.load(ident)
        state = self.store.result_set('admit', {'assets': [plan['hide']]})
        return self.move(state, plan, restore=True, apply=apply)

    def move(self, state: dict[str, Any], plan: dict[str, Any], *, restore: bool, apply: bool) -> dict[str, Any]:
        from library.primitives import WorkingState

        ident, key, admission = (plan[k] for k in ('id', 'hide', 'admission'))
        saved = self.journal(ident)
        retained = saved.parent / 'copy'
        exposed = admission_path(self.root, admission['target'])
        asset = state['assets'][key]
        marker = {'signature': admission['signature'], 'duplicate_of': plan['keep'],
                  'default_operation': plan['kept_operation'], 'journal': ident}
        current, selection = asset.get('admission'), asset.get('reader_selection', {})
        before = current == admission and selection == plan['previous']
        after = current is None and selection == marker
        if (not (before or after) or retained.is_symlink()
                or exposed.exists() == retained.exists()):
            raise LibraryError('Reader selection diverged from its journal')
        if (saved.exists() and self.load(ident) != plan) or (retained.exists() and not saved.exists()):
            raise LibraryError('Reader recovery does not match the saved selection')
        verify_admission(self.operations, key, admission)
        source, target = (retained, exposed) if restore else (exposed, retained)
        verify(source if source.exists() else target, admission['manifest'])
        response = {'status': 'dry-run', 'keep': plan['keep'], 'hide': key, 'journal': ident,
                    'source': str(source), 'target': str(target)}
        if not apply:
            return response
        if self.operations.pending_classified():
            raise LibraryError('Reconcile pending classified copies before selection')
        if not isinstance(state, WorkingState) or state.conflict is None:
            raise LibraryError('Reader selection requires scoped catalogue authority')
        self.store.verify_result(state.conflict)
        private_tree(self.root)
        if not saved.exists():
            saved.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            atomic_yaml(saved, plan, exclusive=True)
            sync_dir(saved.parent.parent)
            sync_dir(saved.parent.parent.parent)
        if restore:
            receipt = saved.with_name('restore-requested.yaml')
            if not receipt.exists():
                atomic_yaml(receipt, {'selection': ident}, exclusive=True)
        if source.exists():
            rename_atomic(source, target, private_root=self.root)
        verify(target, admission['manifest'])
        if source.exists():
            raise LibraryError('Reader selection did not move the complete directory')
        if restore and after:
            asset['admission'] = admission
            asset.pop('reader_selection')
            if plan['previous']:
                asset['reader_selection'] = plan['previous']
        elif not restore and before:
            del asset['admission']
            asset['reader_selection'] = marker
        if (restore and after) or (not restore and before):
            self.store.save(state, expected=state['revision'])
        readback = self.store.backend().get('assets', key, media=False) or {}
        if (readback.get('admission') != (admission if restore else None)
                or readback.get('reader_selection', {}) != (plan['previous'] if restore else marker)):
            raise LibraryError('Reader selection catalogue readback failed')
        response['status'] = 'restored' if restore else 'retained'
        return response
