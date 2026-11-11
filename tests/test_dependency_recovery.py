"""Only an observed failed-to-ready transition starts a dependency episode."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)


class DependencyRecoveryTests(unittest.TestCase):
    def test_success_before_failure_and_restarts_do_not_create_recovery(self):
        from library.dependency_recovery import DependencyRecovery
        with tempfile.TemporaryDirectory() as directory:
            from tests.postgres_fixture import create_store
            from library.primitives import empty
            create_store(self,Path(directory),empty())
            path = Path(directory)/'dependencies.yaml'
            recovery = DependencyRecovery.create(path)
            recovery.observe('openlibrary.org', available=True, at=10)
            self.assertEqual(recovery.proof({'openlibrary.org': 11}), '')
            recovery.observe('openlibrary.org', available=False, at=11)
            recovery = DependencyRecovery(path)
            recovery.observe('openlibrary.org', available=False, at=12)
            self.assertEqual(recovery.proof({'openlibrary.org': 11}), '')
            recovery.observe('openlibrary.org', available=True, at=13)
            first = recovery.proof({'openlibrary.org': 11})
            self.assertTrue(first)
            recovery = DependencyRecovery(path)
            recovery.observe('openlibrary.org', available=True, at=14)
            self.assertEqual(recovery.proof({'openlibrary.org': 11}), first)
            self.assertEqual(recovery.proof({'openlibrary.org': 15}), '')
            self.assertEqual(recovery.proof({'openlibrary.org': 11, 'catalogue.bnf.fr': 11}), '')
