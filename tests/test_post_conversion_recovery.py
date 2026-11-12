"""Current PostgreSQL state retains decisions, copies, admission and retirement."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from library.catalog import apply_effect
from library.primitives import LibraryError
from library.migration import recover_yaml
from library.recovery import checkpoint, verify_checkpoint
from library.resolution import Resolution
from library.publish import Publisher, Capacity
from library.admission import AdmissionPublisher
from library.retirement import Retirement
from test_retirement import fixture
from test_library_intake import SOURCE


class PostConversionRecoveryTests(unittest.TestCase):
    def test_new_decision_copies_admission_and_retirement_reconcile_with_exact_legacy(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'fixture'; root.mkdir()
            catalog, reception, collector, original, client = fixture(root,self)
            state = catalog.read(); asset = next(iter(state['assets']))
            apply_effect(state, {'action': 'identify', 'target': asset, 'values': {
                'work_id': 'work', 'edition_id': 'edition', 'work': {'title': 'Album'},
                'edition': {'publisher': 'Publisher', 'language': 'fr', 'evidence': {'operator': 'fixture'}}}})
            catalog.save(state, expected=state['revision'])
            store = catalog.backend()
            resolution = Resolution(catalog, reception)
            page = resolution.page(asset, 1)
            preview = resolution.preview(asset, {'edition_id': 'edition', 'work_id': 'work',
                'work': {}, 'edition': {}, 'select': True, 'proof': [
                    {'page': page['id'], 'field': field, 'text': 'Checked visible ' + field}
                    for field in ('title', 'publisher', 'language')],
                'reason': 'Checked retained edition', 'supersedes': None, 'restore': None})
            resolution.confirm({'request_id': 'post-conversion', 'preview': preview['id']})
            retirement = Retirement(client, catalog, reception, collector / 'monitor.yaml', collector / 'state.yaml')
            self.assertEqual(retirement.process(SOURCE, apply=True)['outcome'], 'retired')
            publisher = Publisher(catalog, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            resolution.apply(publisher)
            self.assertEqual(catalog.get('assets', asset)['resolution']['status'], 'applied')
            admission = AdmissionPublisher(catalog, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            self.assertEqual(admission.publish_asset(asset)['phase'], 'committed')
            before = store.export()
            decisions = catalog.ledger()
            from library.catalog import Catalog
            from library.operational_state import CopyOperations, NativeHistory
            publications = CopyOperations(store._database, "publication").records()
            admissions = CopyOperations(store._database, "admission").records()
            retired = NativeHistory(catalog.directory).retirements()
            with self.assertRaises(LibraryError): recover_yaml(catalog.directory)
            reopened = Catalog(catalog.directory)
            self.assertEqual(reopened.read(), before)
            self.assertEqual(reopened.ledger(), decisions)
            reopened.project_pending()
            self.assertEqual(reopened.read(), before)
            self.assertEqual(CopyOperations(reopened.backend()._database, "publication").records(), publications)
            self.assertEqual(CopyOperations(reopened.backend()._database, "admission").records(), admissions)
            self.assertEqual(NativeHistory(reopened.directory).retirements(), retired)
            self.assertTrue(original.is_file()); self.assertEqual(client.removed, [1])
