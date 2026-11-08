import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from library.catalog import Catalog, LibraryError, lock
from library.automatic import process_assets
from library.metadata import Providers
from library.intake import receive
from test_library_publish import setup_library


class AutomaticTests(unittest.TestCase):
    def test_changed_reader_policy_retries_terminal_format_once_without_metadata_reset(self):
        from library.readers import READER_POLICY, fingerprint
        from test_library_intake import SOURCE
        with tempfile.TemporaryDirectory() as folder:
            store, reception, original, asset = setup_library(Path(folder))
            state = store.read()
            for origin in state['origins'].values():
                origin.pop('reader_policy', None)
                origin.update(availability='rejected', reason='LibraryError: Active PDF content or attachment',
                              fingerprint=list(fingerprint(original)))
            store.save(state, expected=state['revision'])
            captured = {'items': {SOURCE: {'title': 'Album'}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                {'fid': 1, 'name': original.name, 'status': 0, 'size': original.stat().st_size}]}]
            with patch('library.intake.inspect_file', return_value={'status': 'rejected',
                       'reason': 'LibraryError: Active PDF content or attachment', 'reader_policy': READER_POLICY}) as inspect:
                for _ in range(2):receive(store, reception, captured, journal, queue, incremental=True)
                self.assertEqual(inspect.call_count, 1)
            self.assertEqual(store.read()['assets'][asset]['identity'], state['assets'][asset]['identity'])

    def test_genuine_multipart_old_policy_is_retried_once_then_cached(self):
        from library.readers import fingerprint, READER_POLICY
        from test_library_intake import SOURCE
        with tempfile.TemporaryDirectory() as folder:
            store, reception, original, asset = setup_library(Path(folder))
            target = original.with_name('album.part01.cbz');original.rename(target)
            state = store.read()
            for origin in state['origins'].values():
                origin.update(path='package/'+target.name, availability='pending', reason='Multipart representation',
                              fingerprint=list(fingerprint(target)))
                origin.pop('reader_policy', None)
            store.save(state, expected=state['revision'])
            captured = {'items': {SOURCE: {'title': 'Album'}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                {'fid': 1, 'name': target.name, 'status': 0, 'size': target.stat().st_size}]}]
            from library.readers import inspect_file
            with patch('library.intake.inspect_file', wraps=inspect_file) as inspect:
                for _ in range(2):receive(store, reception, captured, journal, queue, incremental=True)
                self.assertEqual(inspect.call_count, 1)
            self.assertEqual(next(iter(store.read()['origins'].values()))['reader_policy'], READER_POLICY)

    def test_old_pending_retries_once_per_method_and_changed_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            store, reception, original, asset = setup_library(Path(folder))
            state = store.read(); state['assets'][asset].pop('identity'); state['assets'][asset]['identity_result'] = {'status': 'pending', 'reason': 'Unknown edition'}
            store.save(state, expected=state['revision'])
            providers = Providers(Path(folder) / 'cache', fetch=lambda url: (404, ''), interval=0, budget=50)
            state=store.read();state['assets'][asset]['printed']={'status':'ok','asset':asset,'pages':[]}
            store.save(state,expected=state['revision'])
            with patch('library.automatic.enrich',side_effect=lambda *args:store.read()) as enrich, patch('library.automatic.scan_printed') as printed:
                for _ in range(2):
                    result = process_assets(store, reception, Path(folder) / 'library', providers)
                self.assertEqual(enrich.call_count,1); printed.assert_not_called()
                state=store.read();state['assets'][asset]['embedded']={'Title':'New source evidence'}
                store.save(state,expected=state['revision'])
                for _ in range(2):
                    result=process_assets(store,reception,Path(folder)/'library',providers)
                self.assertEqual(enrich.call_count,2)
            self.assertEqual(result['backlog'], 0)
            self.assertEqual(store.read()['assets'][asset]['automation']['outcome'], 'pending')

    def test_temporary_failure_retries_are_capped_and_budget_deferral_does_not_spend_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            store, reception, original, asset = setup_library(Path(folder))
            state = store.read(); state['assets'][asset].pop('identity'); store.save(state, expected=state['revision'])
            providers = Providers(Path(folder) / 'cache', fetch=lambda url: (404, ''), interval=0, budget=0)
            result = process_assets(store, reception, Path(folder) / 'library', providers, clock=lambda: 0)
            self.assertEqual(result['backlog'], 1)
            self.assertEqual(store.read()['assets'][asset].get('automation', {}).get('attempts', 0), 0)
            providers.budget = 50
            with patch('library.automatic.scan_printed', side_effect=OSError('temporary engine failure')):
                for now in (0, 299, 300, 1199, 1200, 9999):
                    process_assets(store, reception, Path(folder) / 'library', providers, clock=lambda: now)
            attempt = store.read()['assets'][asset]['automation']
            self.assertEqual(attempt['attempts'], 3)
            self.assertEqual(attempt['outcome'], 'unavailable')

    def test_incremental_intake_reuses_validated_fingerprint_and_detects_change(self):
        with tempfile.TemporaryDirectory() as folder:
            store, reception, original, asset = setup_library(Path(folder))
            from test_library_intake import SOURCE
            captured = {'items': {SOURCE: {'title': 'Album'}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                {'fid': 1, 'name': original.name, 'status': 0, 'size': original.stat().st_size}]}]
            receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
            with patch('library.intake.inspect_file', side_effect=AssertionError('unchanged source read again')):
                receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
            original.touch()
            with patch('library.intake.inspect_file', wraps=__import__('library.readers', fromlist=['inspect_file']).inspect_file) as inspect:
                receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
                self.assertEqual(inspect.call_count, 1)

    def test_metadata_transport_retry_differs_from_uncertain_identity(self):
        for status, expected in ((503, 'retry'), (404, 'pending')):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                store, reception, original, asset = setup_library(Path(folder))
                state = store.read();state['assets'][asset].pop('identity')
                state['assets'][asset]['printed'] = {'status': 'ok'}
                store.save(state, expected=state['revision'])
                providers = Providers(Path(folder) / 'cache', fetch=lambda url: (404, ''), interval=0, budget=50)
                def metadata(*args):
                    providers.events.append({'status': 'unavailable', 'http_status': status})
                    current = store.read();current['assets'][asset]['identity_result'] = {'status': 'pending', 'reason': 'Unknown edition'}
                    store.save(current, expected=current['revision']);return store.read()
                with patch('library.automatic.enrich', side_effect=metadata):
                    process_assets(store, reception, Path(folder) / 'library', providers, clock=lambda: 0)
                self.assertEqual(store.read()['assets'][asset]['automation']['outcome'], expected)

    def test_actual_reader_timeout_is_retry_but_source_drift_is_terminal_pending(self):
        for error, expected in ((LibraryError('Printed reader exceeded wall time; owned engine stopped'), 'retry'),
                                (LibraryError('Printed evidence source changed'), 'pending')):
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as folder:
                store, reception, original, asset = setup_library(Path(folder))
                state = store.read(); state['assets'][asset].pop('identity'); store.save(state, expected=state['revision'])
                providers = Providers(Path(folder) / 'cache', fetch=lambda url: (404, ''), interval=0, budget=50)
                with patch('library.automatic.scan_printed', side_effect=error):
                    process_assets(store, reception, Path(folder) / 'library', providers, clock=lambda: 0)
                record = store.read()['assets'][asset]['automation']
                self.assertEqual(record['outcome'], expected)
                self.assertEqual(record['attempts'], 1)
                self.assertEqual(record['next_attempt'], 300)

    def test_migration_reuses_only_identical_known_validated_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            store, reception, original, asset = setup_library(Path(folder))
            from test_library_intake import SOURCE
            state = store.read()
            for origin in state['origins'].values(): origin.pop('fingerprint', None)
            store.save(state, expected=state['revision'])
            captured = {'items': {SOURCE: {'title': 'Album'}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                {'fid': 1, 'name': original.name, 'status': 0, 'size': original.stat().st_size}]}]
            with patch('library.intake.inspect_file', side_effect=AssertionError('known bytes were decoded')):
                receive(store, reception, captured, journal, queue, incremental=True)
            migrated = store.read();self.assertTrue(next(iter(migrated['origins'].values()))['fingerprint'])
            for origin in migrated['origins'].values():
                origin.update(sha256=None, availability='pending', reason='Intake budget deferred')
                origin.pop('fingerprint', None)
            store.save(migrated, expected=migrated['revision'])
            with patch('library.intake.inspect_file', side_effect=AssertionError('canonical inverse bytes were decoded')):
                receive(store, reception, captured, journal, queue, incremental=True)
            migrated = store.read()
            self.assertEqual(next(iter(migrated['origins'].values()))['sha256'], asset)
            import copy
            ambiguous = copy.deepcopy(migrated)
            ambiguous['assets']['b'*64] = copy.deepcopy(ambiguous['assets'][asset])
            ambiguous['assets']['b'*64]['sha256'] = 'b'*64
            for origin in ambiguous['origins'].values():
                origin.update(sha256=None, availability='pending', reason='Intake budget deferred')
                origin.pop('fingerprint', None)
            store.save(ambiguous, expected=ambiguous['revision'])
            with patch('library.intake.inspect_file', side_effect=AssertionError('ambiguous inverse was admitted')):
                for _ in range(2):
                    receive(store, reception, captured, journal, queue, incremental=True)
            self.assertIsNone(next(iter(store.read()['origins'].values()))['sha256'])
            migrated['revision'] = store.read()['revision']
            for origin in migrated['origins'].values():origin.pop('fingerprint', None)
            store.save(migrated, expected=migrated['revision'])
            for origin in migrated['origins'].values():
                origin.update(sha256=None, availability='pending', reason='Intake budget deferred')
            migrated['revision'] = store.read()['revision']
            store.save(migrated, expected=migrated['revision'])
            original.write_bytes(original.read_bytes()[:-1] + b'X')
            with patch('library.intake.inspect_file', return_value={'status': 'validated', 'sha256': 'b'*64}) as inspected:
                receive(store, reception, captured, journal, queue, incremental=True)
                self.assertEqual(inspected.call_count, 1)
                receive(store, reception, captured, journal, queue, incremental=True)
            self.assertEqual(set(store.read()['assets']), {asset})
            self.assertEqual(next(iter(store.read()['origins'].values()))['availability'], 'rejected')

    def test_budget_deferral_preserves_source_identity_across_changed_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            store, reception, original, asset = setup_library(Path(folder))
            from test_library_intake import SOURCE
            captured = {'items': {SOURCE: {'title': 'Album'}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                {'fid': 1, 'name': original.name, 'status': 0, 'size': original.stat().st_size}]}]
            original.write_bytes(original.read_bytes()[:-1] + b'X')
            receive(store, reception, captured, journal, queue, incremental=True, inspection_limit=0)
            self.assertEqual(next(iter(store.read()['origins'].values()))['sha256'], asset)
            with patch('library.intake.inspect_file', return_value={'status': 'rejected', 'reason': 'Corrupt input'}):
                receive(store, reception, captured, journal, queue, incremental=True, inspection_limit=8)
            self.assertEqual(next(iter(store.read()['origins'].values()))['sha256'], asset)
            original.touch()
            with patch('library.intake.inspect_file', return_value={'status': 'validated', 'sha256': 'b'*64}):
                for _ in range(2):
                    receive(store, reception, captured, journal, queue, incremental=True, inspection_limit=8)
            self.assertEqual(set(store.read()['assets']), {asset})
            self.assertEqual(next(iter(store.read()['origins'].values()))['sha256'], asset)
            self.assertEqual(next(iter(store.read()['origins'].values()))['availability'], 'rejected')

    def test_failed_publication_is_recovery_work_even_after_method_change(self):
        from library.automatic import ready_assets
        with tempfile.TemporaryDirectory() as folder:
            store,_,_,asset=setup_library(Path(folder))
            state=store.read();state['assets'][asset]['automation']={'outcome':'unavailable',
                'method':'old-method','reason':'Publication failed; inspect journal before retry'}
            store.save(state,expected=state['revision'])
            self.assertEqual(ready_assets(store),0)
