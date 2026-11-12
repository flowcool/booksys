"Cached deferrals use the evidence expiry; task allowances stay intact."
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from library.business_stages import BusinessStages
from library.stage_model import Task, Obligation

class MetadataDeferDeadlineTests(unittest.TestCase):
    def test_cached_budget_failure_waits_until_evidence_expiry(self):
        stamp = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        expiry = stamp.timestamp()+300
        obligation = Obligation('metadata', 'asset', 'revision', 'metadata-step-v1')
        task = Task(obligation.key, 'metadata', 'asset', 'revision',
                    'metadata-step-v1', stamp.isoformat(), stamp.isoformat())
        providers = SimpleNamespace(events=[], admission=None)
        catalog = SimpleNamespace(prepare=lambda state:'prepared')
        business = BusinessStages(catalog, Path('/unused'), Path('/unused'), providers)
        business.obligations = lambda *args, **kwargs:[]
        def enrich(*args):
            providers.events.append({'status':'unavailable', 'reason':'Request budget exhausted',
                                     'expires':expiry})
            return {'assets':{'asset':{}}}
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):return stamp
        with patch('library.metadata.prepare_enrichment', side_effect=enrich), patch(
                'library.business_stages.evidence_version', return_value='evidence'), patch(
                'library.business_stages.datetime', Clock):
            result = business._execute(task, now=stamp)
        self.assertEqual(result.outcome, 'defer')
        self.assertEqual(result.due_at.timestamp(), expiry)

    def test_provider_events_retain_failure_expiry(self):
        from library.metadata import Providers
        providers = object.__new__(Providers)
        providers.shared, providers.events = None, []
        providers._get = lambda url:{'status':'unavailable','reason':'Request budget exhausted','expires':1234}
        providers.get('https://openlibrary.org/books/OL1M.json')
        self.assertEqual(providers.events[0]['expires'], 1234)
