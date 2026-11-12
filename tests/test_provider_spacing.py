"Offline redirect pacing. No database or network."
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request
import library.metadata as metadata
from booksys_core.network_admission import BudgetWait

class ProviderSpacingTests(unittest.TestCase):
    def provider(self, *, requests=50):
        clock = SimpleNamespace(value=100.0, sleeps=[])
        def sleep(seconds):
            clock.sleeps.append(seconds)
            clock.value += seconds
        starts = []
        class Budget:
            path = Path('/unused/provider-admission.yaml')
            def debit(self):
                due = starts[-1]+1 if starts else 0
                if len(starts) >= requests:
                    due = max(due, starts[0]+300)
                if clock.value < due:
                    raise BudgetWait(due)
                starts.append(clock.value)
        cache = {}
        provider = object.__new__(metadata.Providers)
        provider.interval, provider.budget = 1, 50
        provider.shared, provider.last_request, provider.events = None, 0, []
        provider.admission = Budget()
        provider._cache = SimpleNamespace(get=cache.get, retain=lambda key,value: cache.update({key:value}))
        def fetch(url):
            metadata.QualifiedRedirect().redirect_request(
                Request(url), None, 302, 'Found', {},
                'https://openlibrary.org/books/OL1M/title.json')
            return 200, '{}'
        provider.fetch = fetch
        self.enterContext(patch.object(metadata, 'time', SimpleNamespace(
            time=lambda:clock.value, monotonic=lambda:clock.value, sleep=sleep)))
        self.enterContext(patch('library.dependency_recovery.DependencyRecovery'))
        return provider, clock, starts

    def test_redirect_waits_for_spacing_and_spends_two_starts(self):
        provider, clock, starts = self.provider()
        result = provider.get('https://openlibrary.org/isbn/9782070368228.json')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(starts, [100.0, 101.0])
        self.assertEqual(clock.sleeps, [0, 1.0])

    def test_window_limit_remains_a_deferral(self):
        provider, clock, starts = self.provider(requests=1)
        result = provider.get('https://openlibrary.org/isbn/9782070368228.json')
        self.assertEqual(result.get('reason'), 'Request budget exhausted')
        self.assertEqual(starts, [100.0])
        self.assertEqual(clock.value, 100.0)
