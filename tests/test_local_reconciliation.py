"""Known local copy failures do not block another asset; unknown scope does."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.primitives import atomic_yaml, LibraryError
from library.publish import Publisher, Capacity
from test_library_publish import setup_library
from postgres_fixture import require_postgresql

class LocalReconciliationTests(unittest.TestCase):
    def test_scoped_admission_does_not_enumerate_unrelated_catalogue_assets(self):
        from library.admission import AdmissionPublisher
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, aid = setup_library(root,self)
            require_postgresql(store)
            writer = AdmissionPublisher(store, reception, root/'library', isolated=True,
                local_recovery=True, capacity=Capacity(reserve=0))
            records = store.records
            def scoped_records(kind, **kwargs):
                self.assertNotEqual(kind, 'assets', 'One-asset recovery must not read every asset')
                return records(kind, **kwargs)
            with patch.object(store, 'records', side_effect=scoped_records):
                self.assertEqual(writer.reconcile(asset=aid), [])

    def test_asset_owned_journals_skip_identity_lookup_after_scope_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, aid = setup_library(root,self)
            require_postgresql(store)
            writer = Publisher(store, reception, root/'library', isolated=True,
                local_recovery=True, capacity=Capacity(reserve=0))
            for kind, target in (('admission', 'holding/title--'+'b'*64+'--'+'c'*12),
                                 ('provisional', 'b'*64)):
                operation = {'kind': kind, 'asset': 'b'*64, 'target': target}
                with patch.object(store, 'working_set', side_effect=AssertionError('Unrelated asset lookup')):
                    self.assertFalse(writer.operation_applies(operation, aid))
                    with self.assertRaisesRegex(LibraryError, 'destination scope'):
                        writer.operation_applies({**operation, 'target': 'unowned'}, aid)

    def test_scoped_admission_still_rejects_output_drift_and_missing_journal(self):
        from library.admission import AdmissionPublisher
        for failure in ('drift', 'missing_journal'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store, reception, _, aid = setup_library(root,self)
                require_postgresql(store)
                writer = AdmissionPublisher(store, reception, root/'library', isolated=True,
                    local_recovery=True, capacity=Capacity(reserve=0))
                operation = writer.publish_asset(aid)
                if failure == 'drift':
                    (root/'library'/operation['target']/'book.cbz').write_bytes(b'changed externally')
                    reason = 'Managed file drift'
                else:
                    # Corrupt only the isolated fixture's canonical journal.
                    with store.backend()._database.connection(write=True) as db:
                        db.execute("DELETE FROM copy_operations WHERE namespace='admission' AND id=%s",
                                   (operation['id'],))
                    reason = 'Admission journal missing'
                with self.assertRaisesRegex(LibraryError, reason):
                    writer.reconcile(asset=aid)

    def test_real_publication_bypasses_known_unrelated_blocked_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, source, aid = setup_library(root,self)
            require_postgresql(store)
            writer = Publisher(store, reception, root/'library', isolated=True, local_recovery=True, capacity=Capacity(reserve=0))
            writer.operations.phase( {'schema': 1, 'id': 'b'*32, 'asset': 'b'*64,
                        'edition': 'other', 'target': 'other--other', 'kind': 'create', 'phase': 'blocked', 'created_at': '2026-11-09'})
            operation = writer.publish(aid)
            self.assertEqual(operation['phase'], 'committed')
            self.assertTrue(source.is_file())
            with self.assertRaisesRegex(LibraryError, 'Blocked operation'):
                writer.reconcile()
            with self.assertRaisesRegex(LibraryError, 'Blocked operation'):
                writer.reconcile(asset='b'*64)

    def test_unknown_journal_and_shared_edition_remain_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, aid = setup_library(root,self)
            require_postgresql(store)
            writer = Publisher(store, reception, root/'library', isolated=True, local_recovery=True, capacity=Capacity(reserve=0))
            path = writer.journals/('b'*32+'.yaml')
            op = {'schema': 1, 'id': 'b'*32, 'asset': 'b'*64, 'edition': 'edition',
                  'target': 'other--edition', 'kind': 'create', 'phase': 'blocked', 'created_at': '2026-11-09'}
            writer.operations.phase(op)
            with self.assertRaisesRegex(LibraryError, 'Blocked operation'):
                writer.publish(aid)
            op.pop('asset')
            writer.operations.phase(op)
            with self.assertRaisesRegex(LibraryError, 'scope'):
                writer.publish(aid)

    def test_claimed_unrelated_asset_cannot_hide_a_conflicting_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, aid = setup_library(root,self)
            require_postgresql(store)
            writer = Publisher(store, reception, root/'library', isolated=True, local_recovery=True, capacity=Capacity(reserve=0))
            writer.operations.phase( {'schema':1,'id':'b'*32,'asset':'b'*64,
                'edition':'other','target':'album--edition','kind':'create','phase':'blocked','created_at':'2026-11-09'})
            with self.assertRaisesRegex(LibraryError, 'destination scope'):
                writer.publish(aid)
