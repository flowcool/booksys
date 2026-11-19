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

    def test_failed_metadata_reopens_once_after_every_dependency_recovers(self):
        import os
        from unittest.mock import patch
        self.enterContext(patch.dict(os.environ, {'BOOKSYS_WORK_STAGES': 'metadata,copies'}))
        from datetime import timedelta
        from library.business_stages import BusinessStages
        from library.dependency_recovery import DependencyRecovery
        from library.stage_tasks import StageTasks
        from library.stage_model import METHODS
        from library.work_items import WorkItems
        from library.automatic import evidence_version
        from library.metadata import Providers
        from test_library_publish import setup_library
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, _, key = setup_library(root, self)
            state = catalog.working_set('identify', {'assets': [key]})
            state['assets'][key]['stage_metadata'] = {
                'evidence': evidence_version(state['assets'][key]), 'at': AT.isoformat(),
                'evaluation': 'failed-evaluation', 'outcome': 'retry', 'method': METHODS['metadata'],
                'failed_dependencies': {'catalogue.bnf.fr': AT.timestamp()}}
            catalog.save(state, expected=state['revision'])
            tasks = StageTasks(catalog.backend())
            work = WorkItems(tasks.store._database, max_attempts={'metadata': 1})
            self.addCleanup(work.close)
            stages = BusinessStages(catalog, reception, root/'library', Providers(root/'cache', fetch=lambda url: (503, ''), interval=0))
            recovery = DependencyRecovery(root/'dependencies.yaml', database=tasks.store._database)
            stages.discover(tasks, now=AT, keys=[key])
            first = work.claim('metadata', 'test', now=AT)
            self.assertIsNotNone(first)
            work.retry(first, 'retry', timedelta(seconds=1), now=AT)
            recovery.observe('catalogue.bnf.fr', available=False, at=AT.timestamp())
            stages.discover(tasks, now=AT+timedelta(seconds=1), keys=[key])
            self.assertIsNone(work.claim('metadata', 'test', now=AT+timedelta(seconds=2)))
            recovery.observe('catalogue.bnf.fr', available=True, at=AT.timestamp()+3)
            stages.discover(tasks, now=AT+timedelta(seconds=4), keys=[key])
            stages.discover(tasks, now=AT+timedelta(seconds=5), keys=[key])
            second = work.claim('metadata', 'test', now=AT+timedelta(seconds=6))
            self.assertIsNotNone(second)
            self.assertEqual(second.attempts, 1)
            self.assertNotEqual(second.input_rev, first.input_rev)
            # A crash before committing stage_metadata still consumes this episode.
            work.retry(second, 'crash', timedelta(seconds=1), now=AT+timedelta(seconds=7))
            stages.discover(tasks, now=AT+timedelta(seconds=8), keys=[key])
            self.assertIsNone(work.claim('metadata', 'test', now=AT+timedelta(seconds=9)))

            # A later real transition permits one more episode, even if the
            # provider returns an old cached failure from before that recovery.
            recovery.observe('catalogue.bnf.fr', available=False, at=AT.timestamp()+10)
            stages.discover(tasks, now=AT+timedelta(seconds=10), keys=[key])
            self.assertIsNone(work.claim('metadata', 'test', now=AT+timedelta(seconds=10)))
            recovery.observe('catalogue.bnf.fr', available=True, at=AT.timestamp()+11)
            stages.discover(tasks, now=AT+timedelta(seconds=12), keys=[key])
            from library.work_actor import WorkActor
            def cached_failure(catalog, asset_id, providers, references):
                providers.events.append({'dependency': 'catalogue.bnf.fr', 'status': 'unavailable',
                                         'http_status': 503, 'retrieved_at': AT.isoformat()})
                return catalog.working_set('identify', {'assets': [asset_id]})
            actor = WorkActor(tasks.store, work, 'metadata', lambda task, stamp: stages.execute(task, now=stamp))
            with patch('library.metadata.prepare_enrichment', side_effect=cached_failure):
                self.assertEqual(actor.step(now=AT+timedelta(seconds=13)), 'retry')
            retained = catalog.get('assets', key)['stage_metadata']
            self.assertTrue(retained['recovery_proof'])
            self.assertEqual(retained['evaluation'], 'recovery:' + retained['recovery_proof'])
            stages.discover(tasks, now=AT+timedelta(seconds=14), keys=[key])
            self.assertIsNone(work.claim('metadata', 'test', now=AT+timedelta(seconds=15)))

            # All failed dependencies must recover. One recovered dependency
            # cannot clear a second outage or a manual queue block.
            state = catalog.working_set('identify', {'assets': [key]})
            state['assets'][key]['stage_metadata']['failed_dependencies']['openlibrary.org'] = AT.timestamp()+16
            catalog.save(state, expected=state['revision'])
            recovery.observe('catalogue.bnf.fr', available=False, at=AT.timestamp()+17)
            recovery.observe('catalogue.bnf.fr', available=True, at=AT.timestamp()+18)
            stages.discover(tasks, now=AT+timedelta(seconds=19), keys=[key])
            self.assertIsNone(work.claim('metadata', 'test', now=AT+timedelta(seconds=20)))
            recovery.observe('openlibrary.org', available=False, at=AT.timestamp()+16)
            recovery.observe('openlibrary.org', available=True, at=AT.timestamp()+21)
            stages.discover(tasks, now=AT+timedelta(seconds=22), keys=[key])
            third = work.claim('metadata', 'test', now=AT+timedelta(seconds=23))
            self.assertIsNotNone(third)
            self.assertNotEqual(third.input_rev, second.input_rev)
            work.retry(third, 'retry', timedelta(seconds=1), now=AT+timedelta(seconds=24))
            # Establish a real manual block through the normal obligation API.
            from library.stage_model import Obligation
            tasks.discover(Obligation('metadata', key, 'blocked-input', METHODS['metadata'],
                                     blocked='remote_review_block'), now=AT+timedelta(seconds=26))
            recovery.observe('catalogue.bnf.fr', available=False, at=AT.timestamp()+27)
            recovery.observe('catalogue.bnf.fr', available=True, at=AT.timestamp()+28)
            stages.discover(tasks, now=AT+timedelta(seconds=29), keys=[key])
            self.assertIsNone(work.claim('metadata', 'test', now=AT+timedelta(seconds=30)))

            # Recovery cannot bring a completed weekly evaluation forward.
            state = catalog.working_set('identify', {'assets': [key]})
            state['assets'][key]['stage_metadata'].update(outcome='done', next_at=(AT+timedelta(days=7)).isoformat())
            self.assertFalse(any(o.stage == 'metadata' for o in stages.obligations(state, key, now=AT+timedelta(seconds=31))))

            # Changed asset evidence uses its normal revision, not an old
            # provider recovery episode. No global method bump is required.
            state['assets'][key]['embedded'] = {'Title': 'New asset-bound evidence'}
            changed = [o for o in stages.obligations(state, key, now=AT+timedelta(seconds=32)) if o.stage == 'metadata']
            self.assertEqual(len(changed), 1)
            self.assertFalse(changed[0].input_revision.startswith('recovery:'))
