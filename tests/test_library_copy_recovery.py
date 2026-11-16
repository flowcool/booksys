"""Forward admission recovery preserves private output and exposed ownership."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from library.admission import AdmissionPublisher, projection
from library.primitives import LibraryError
from library.publication import manifest
from test_library_publish import setup_library


class CopyRecoveryTests(unittest.TestCase):
    def test_stale_unexposed_output_is_retained_and_fresh_admission_is_published(self):
        for phase in ('prepared', 'verified'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                catalog, reception, _, asset = setup_library(root, self)
                def crash(at, operation):
                    if at == phase:
                        raise RuntimeError('interrupted before exposure')
                writer = AdmissionPublisher(catalog, reception, root/'library', isolated=True,
                                            local_recovery=True, fault=crash)
                with self.assertRaisesRegex(RuntimeError, 'before exposure'):
                    writer.publish_asset(asset)
                old = writer.operations.for_asset(asset, writer.operation_applies)[0]
                retained = manifest(writer.staging/old['id'])
                state = catalog.read()
                state['assets'][asset]['overrides'] = {'title': 'Current title'}
                catalog.save(state, expected=state['revision'])
                fresh = AdmissionPublisher(catalog, reception, root/'library', isolated=True,
                                           local_recovery=True).publish_asset(asset)
                self.assertNotEqual(fresh['id'], old['id'])
                self.assertEqual(fresh['phase'], 'committed')
                current = catalog.get('assets', asset)['admission']
                self.assertEqual(current['operation'], fresh['id'])
                self.assertEqual(current['target'], projection(catalog.read(), asset)['target'])
                self.assertFalse((writer.root/old['target']).exists())
                self.assertEqual(manifest(writer.staging/old['id']), retained)
                retired = writer.operations.get(old['id'])
                self.assertEqual(retired, {**old, 'phase': 'interrupted'})

    def test_stale_exposed_output_keeps_its_journal_and_blocks_fresh_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog, reception, _, asset = setup_library(root, self)
            def crash(at, operation):
                if at == 'after_substitution':
                    raise RuntimeError('interrupted after exposure')
            writer = AdmissionPublisher(catalog, reception, root/'library', isolated=True,
                                        local_recovery=True, fault=crash)
            with self.assertRaisesRegex(RuntimeError, 'after exposure'):
                writer.publish_asset(asset)
            old = writer.operations.for_asset(asset, writer.operation_applies)[0]
            retained = manifest(writer.root/old['target'])
            state = catalog.read()
            state['assets'][asset]['overrides'] = {'title': 'Current title'}
            catalog.save(state, expected=state['revision'])
            with self.assertRaisesRegex(LibraryError, 'relevant inputs changed'):
                writer.reconcile(asset=asset)
            self.assertEqual(writer.operations.get(old['id']), old)
            self.assertEqual(manifest(writer.root/old['target']), retained)
            self.assertIsNone(catalog.get('assets', asset).get('admission'))

    def test_decoder_failure_retains_exit_or_signal_without_untrusted_stderr(self):
        from unittest.mock import Mock
        from library.decode import run
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/'source.cbz'
            source.write_bytes(b'retained source')
            for code, diagnostic in ((1, 'exit code 1'), (-9, 'signal 9')):
                with self.subTest(code=code):
                    process = Mock(returncode=code)
                    process.communicate.return_value = (b'', None)
                    with patch('library.decode.WORK_ROOT', str(root)), patch('library.decode.subprocess.Popen', return_value=process):
                        with self.assertRaisesRegex(LibraryError, diagnostic):
                            run('transform', source)
            self.assertEqual(source.read_bytes(), b'retained source')
