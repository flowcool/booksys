import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from library.catalog import atomic_yaml, digest
from library.readers import fingerprint
from ops.retry_library_formats import main
from test_library_intake import SOURCE
from test_library_publish import setup_library


class FormatRetryTests(unittest.TestCase):
    def test_exact_retry_preserves_other_origins_and_metadata_and_caches_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, asset = setup_library(root)
            state = store.read()
            oid = next(iter(state['origins']))
            state['origins'][oid].update(availability='rejected',
                reason='LibraryError: Nested container or unauthorized archive content',
                fingerprint=list(fingerprint(source)))
            state['origins'][oid].pop('reader_policy', None)
            other_source = SOURCE + '-other'
            other_id = digest({'source': other_source, 'pid': 2, 'fid': 2})
            other = {**copy.deepcopy(state['origins'][oid]), 'source': other_source, 'pid': 2, 'fid': 2}
            state['origins'][other_id] = other
            state['assets'][asset]['origins'].append(other_id)
            sibling_id = digest({'source': SOURCE, 'pid': 1, 'fid': 3})
            state['origins'][sibling_id] = {**copy.deepcopy(state['origins'][oid]), 'fid': 3}
            state['assets'][asset]['origins'].append(sibling_id)
            store.save(state, expected=state['revision'])
            atomic_yaml(root / 'snapshots/captured.yaml', {'items': {SOURCE: {'title': 'Album'}, other_source: {}}})
            atomic_yaml(root / 'snapshots/submissions.yaml', {'submissions': {
                SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1},
                other_source: {'status': 'queued', 'name': 'other', 'package_id': 2}}})
            atomic_yaml(root / 'snapshots/queue.yaml', [
                {'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                    {'fid': 1, 'name': source.name, 'status': 0, 'size': source.stat().st_size}]},
                {'pid': 2, 'name': 'other', 'folder': 'other', 'links': []}])
            queue_path = root / 'snapshots/queue.yaml'
            journal_path = root / 'snapshots/submissions.yaml'
            saved_queue = queue_path.read_bytes();saved_journal = journal_path.read_bytes()
            for drift in ('missing-package', 'resubmitted'):
                if drift == 'missing-package':
                    atomic_yaml(queue_path, [])
                else:
                    atomic_yaml(journal_path, {'submissions': {SOURCE: {
                        'status': 'queued', 'name': 'other', 'package_id': 2}}})
                before = store.read()
                with patch.object(sys, 'argv', ['retry', '--root', str(root), '--reception', str(reception), '--origin', oid]):
                    with self.assertRaisesRegex(ValueError, 'differs from saved'):
                        main()
                self.assertEqual(store.read(), before)
                queue_path.write_bytes(saved_queue);journal_path.write_bytes(saved_journal)
            from library.readers import inspect_file
            with patch.object(sys, 'argv', ['retry', '--root', str(root), '--reception', str(reception), '--origin', oid]), \
                    patch('library.intake.inspect_file', wraps=inspect_file) as inspection:
                for _ in range(2):self.assertEqual(main(), 0)
                self.assertEqual(inspection.call_count, 1)
            current = store.read()
            self.assertEqual(current['origins'][oid]['availability'], 'validated')
            self.assertEqual(current['origins'][other_id], other)
            self.assertEqual(current['assets'][asset]['identity'], state['assets'][asset]['identity'])
