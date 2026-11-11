"""Publication entry interface and reconciliation across publication kinds."""
from __future__ import annotations
from typing import Any
from library.primitives import lock
from library.publication import (Publisher as Publication, Capacity as Capacity,
    manifest as manifest, private_tree as private_tree, rename_atomic as rename_atomic,
    verify as verify, fiche_for as fiche_for)


class Publisher(Publication):
    def commit(self, operation: dict[str, Any]) -> None:
        super().commit(operation)


    def reconcile(self, *, locked: bool = False, provisional: bool = True, asset: str | None = None) -> list[dict[str, Any]]:
        if not locked:
            with lock(self.store.directory / 'library-copy.lock', blocking=False):
                return self.reconcile(locked=True, provisional=provisional, asset=asset)
        if provisional:
            # An admission can have moved a committed legacy folder to recovery
            # before its catalogue save. Finish that journal before checking the
            # preceding edition exposure. Admission's own preflight passes False.
            from library.admission import AdmissionPublisher
            AdmissionPublisher(self.store, self.reception, self.root, exclusive=True,
                               capacity=self.capacity,prepared=self.prepared).reconcile(locked=True, asset=asset)
        results = super().reconcile(locked=True, provisional=False, asset=asset)
        if provisional:
            from library.provisional import ProvisionalPublisher, retire_provisional
            for _, edition in self.store.records('editions'):
                if asset is not None and edition.get('publication', {}).get('asset') != asset:
                    continue
                if edition.get('publication') and self.prepared is None:
                    verify(self.root / 'bd' / edition['publication']['target'], edition['publication']['manifest'])
                    retire_provisional(self.store, self.root, edition['publication']['asset'])
            ProvisionalPublisher(self.store, self.reception, self.root, exclusive=True, capacity=self.capacity,prepared=self.prepared).reconcile(locked=True, asset=asset)
        return results
