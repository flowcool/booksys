"""Durable backfill CLI: preserve completed evidence and reject active requests."""
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from booksys import DEFAULT_CATEGORY
from postgres_fixture import create_store
from library.primitives import empty
from library.operational_state import CollectorState


class BackfillRequestTests(unittest.TestCase):
    def test_completed_batch_is_retained_before_fifty_page_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store=create_store(self,directory,empty())
            state=CollectorState(directory)
            previous = b'version: 1\nstatus: complete\npages_requested: 10\npages: {}\nitems: {}\nsubmitted: 200\n'
            target = directory / 'backfill.yaml'
            target.write_bytes(previous)
            state.start_backfill(yaml.safe_load(previous))
            tree = ast.parse(Path('ops/start_backfill.py').read_text())
            self.assertTrue(any(isinstance(n, ast.If) and '__name__' in ast.unparse(n.test) for n in tree.body))
            self.assertFalse(any(isinstance(n, ast.With) for n in tree.body))
            environment = {**os.environ, 'BEADS_DIR': str(directory / 'nonexistent-beads')}
            result = subprocess.run([sys.executable, 'ops/start_backfill.py', '--directory', str(directory),
                                     '--pages', '50', '--archive-completed'], capture_output=True, text=True, env=environment)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            archives = list((directory / 'backfill-history').glob('*.yaml'))
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].read_bytes(), previous)
            self.assertEqual(archives[0].stat().st_mode & 0o777, 0o400)
            job = state.backfill()
            self.assertEqual((job['pages_requested'], job['next_page'], job['status']),
                             (50, DEFAULT_CATEGORY, 'pending'))
            self.assertEqual(target.read_bytes(),previous)
            self.assertEqual(len(list((directory/'backfill-requests').glob('*.yaml'))),1)
            first = target.read_bytes()
            retry = subprocess.run([sys.executable, 'ops/start_backfill.py', '--directory', str(directory),
                                    '--pages', '50', '--archive-completed'], capture_output=True, text=True, env=environment)
            self.assertEqual(retry.returncode, 2)
            self.assertEqual(target.read_bytes(), first)
            self.assertEqual(len(list((directory / 'backfill-history').glob('*.yaml'))), 1)

    def test_invalid_count_and_unapproved_replacement_preserve_request(self):
        from ops.start_backfill import record_request
        from booksys import CollectionError
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store=create_store(self,directory,empty())
            state=CollectorState(directory)
            for pages in (0, 51, True):
                with self.assertRaises(CollectionError):
                    record_request(directory, pages)
            self.assertFalse((directory / 'backfill.yaml').exists())
            original = b'version: 1\nstatus: complete\n'
            (directory / 'backfill.yaml').write_bytes(original)
            state.start_backfill(yaml.safe_load(original))
            with self.assertRaises(CollectionError):
                record_request(directory, 50)
            self.assertEqual((directory / 'backfill.yaml').read_bytes(), original)
            self.assertFalse((directory / 'backfill-history').exists())

    def test_archive_retry_after_interrupted_canonical_write_preserves_history(self):
        from ops.start_backfill import record_request
        from unittest.mock import patch
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store=create_store(self,directory,empty())
            state=CollectorState(directory)
            previous = b'version: 1\nstatus: complete\n'
            (directory / 'backfill.yaml').write_bytes(previous)
            state.start_backfill(yaml.safe_load(previous))
            with patch.object(CollectorState,'start_backfill',side_effect=OSError('Interrupted')):
                with self.assertRaises(OSError):
                    record_request(directory, 50, archive_completed=True)
            self.assertEqual((directory / 'backfill.yaml').read_bytes(), previous)
            archive = directory / 'backfill-history' / (hashlib.sha256(previous).hexdigest() + '.yaml')
            self.assertEqual(archive.read_bytes(), previous)
            record_request(directory, 50, archive_completed=True)
            self.assertEqual(len(list(archive.parent.glob('*.yaml'))), 1)
