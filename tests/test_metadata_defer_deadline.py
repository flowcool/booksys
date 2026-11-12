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
        providers._get = lambda url:{'status':'unavailable','reason':'Request budget exhausted','expires':1234}
        providers.get('https://openlibrary.org/books/OL1M.json')
        self.assertEqual(providers.events[0]['expires'], 1234)
