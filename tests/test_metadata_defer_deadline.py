"Cached deferrals use the evidence expiry; task allowances stay intact."
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from library.business_stages import BusinessStages
from library.stage_model import Task, Obligation

class MetadataDeferDeadlineTests(unittest.TestCase):

    def test_provider_events_retain_failure_expiry(self):
        from library.metadata import Providers
        providers = object.__new__(Providers)
        providers.shared, providers.events = None, []
        providers._get = lambda url, **kwargs:{'status':'unavailable','reason':'Request budget exhausted','expires':1234}
        providers.get('https://openlibrary.org/books/OL1M.json')
        self.assertEqual(providers.events[0]['expires'], 1234)


    def test_bdnex_cooldown_defers_without_retrying_the_next_dossier(self):
        from library.bdnex import BDneXClient

        first, second = 'a' * 64, 'b' * 64
        client = BDneXClient('http://127.0.0.1:1', 'offline-token')
        providers = SimpleNamespace(events=[], admission=None, bdnex=client)
        catalog = SimpleNamespace(prepare=lambda state: None)
        stage = BusinessStages(catalog, Path('.'), Path('.'), providers)
        now = datetime.fromtimestamp(1000, timezone.utc)
        other_events = []
        result = 'no_match'

        def enrich(store, asset, providers, references, **kwargs):
            response = client.match(asset, [{'path': 'Example 01.cbz', 'embedded': {}}])
            providers.events.extend([dict(response, retrieved_at=now.isoformat()), *other_events])
            return {'assets': {asset: {'work_result': {'status': result}}}}

        def execute(asset):
            obligation = Obligation('metadata', asset, 'test-revision', 'test-method')
            task = Task(obligation.key, 'metadata', asset, 'test-revision', 'test-method', None, now.isoformat())
            return stage.execute(task, now=now)

        with (
            patch('library.bdnex.build_opener') as opener,
            patch('library.bdnex.time.monotonic', side_effect=[100, 101, 102, 102, 102, 102]),
            patch('library.bdnex.time.time', return_value=1000),
            patch('library.metadata.prepare_enrichment', side_effect=enrich),
            patch('library.business_stages.evidence_version', return_value='test-evidence'),
            patch.object(stage, 'obligations', return_value=[]),
        ):
            opener.return_value.open.side_effect = TimeoutError()
            timed_out = execute(first)
            deferred = execute(second)
            self.assertEqual(timed_out.outcome, 'retry')
            self.assertEqual(deferred.outcome, 'defer')
            self.assertEqual(deferred.due_at, datetime.fromtimestamp(1059, timezone.utc))
            self.assertEqual(deferred.diagnostic['reason'], 'provider_cooldown')

            other_events.append({'status': 'unavailable', 'dependency': 'openlibrary.org',
                                 'reason': 'Timed out', 'exception_type': 'TimeoutError'})
            failed = execute(second)
            self.assertEqual(failed.outcome, 'retry')
            self.assertEqual(failed.diagnostic['dependency'], 'openlibrary.org')

            other_events.clear()
            result = 'identified'
            self.assertEqual(execute(second).outcome, 'done')
            self.assertEqual(opener.return_value.open.call_count, 1)
