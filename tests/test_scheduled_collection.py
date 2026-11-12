"""Scheduler integration releases business locks on deferred browser work."""
from pathlib import Path
import tempfile
import unittest


from booksys_core.browser_outcomes import BrowserOutcome, Outcome
from booksys_core.collection import run_once
from booksys_core.scheduler import Deferred
from library.catalog import lock
from postgres_fixture import create_store
from library.primitives import empty
from library.operational_state import CollectorState
from test_pipeline import seed_monitor


class ScheduledCollectionTests(unittest.TestCase):
    def test_healthy_schedule_does_not_take_the_collector_lock_for_recovery(self):
        from booksys_core.scheduler import ScheduledCalls
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_store(self, root, empty())
            browser = Mock()
            with patch('library.primitives.lock', side_effect=AssertionError('unexpected recovery lock')):
                self.assertFalse(ScheduledCalls(root, browser, clock=lambda: 1000).recover_cleanup())
            browser.check_session.assert_not_called()

    def test_cleanup_recovery_is_receipted_bounded_and_does_not_release_remote_review(self):
        from booksys_core.scheduler import Schedule, ScheduledCalls
        from unittest.mock import Mock
        for healthy in (True, False):
            with self.subTest(healthy=healthy), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                create_store(self, root, empty())
                state = CollectorState(root)
                schedule = Schedule({}, timestamp=1000)
                schedule.record_outcome('source', Outcome('cleanup_failed', 'local'), timestamp=1000)
                state.patch_monitor(schedule=schedule.record)
                now = [1900]
                browser = Mock()
                browser.check_session.side_effect = None if healthy else BrowserOutcome(Outcome('cleanup_failed', 'local'))
                browser.check_session.return_value = 'byparr-session:qualified'
                calls = ScheduledCalls(root, browser, clock=lambda: now[0])
                self.assertEqual(calls.recover_cleanup(), healthy)
                saved = state.monitor()['schedule']
                self.assertEqual(saved['review_block'], not healthy)
                self.assertEqual(saved['cleanup_recovery']['status'], 'qualified' if healthy else 'failed')
                self.assertEqual(browser.check_session.call_count, 1)
                self.assertFalse(calls.recover_cleanup())
                self.assertEqual(browser.check_session.call_count, 1)
                if not healthy:
                    now[0] = 2800
                    self.assertFalse(calls.recover_cleanup())
                    self.assertEqual(browser.check_session.call_count, 2)
                schedule.record_outcome('source', Outcome('remote_rate_limited', 'remote', 'unrepresentable'), timestamp=now[0])
                state.patch_monitor(schedule=schedule.record)
                now[0] += 900
                self.assertFalse(calls.recover_cleanup())
                self.assertTrue(state.monitor()['schedule']['review_block'])

    def test_cleanup_probe_preserves_remote_retry_after(self):
        from booksys_core.scheduler import Schedule, ScheduledCalls
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_store(self, root, empty())
            state = CollectorState(root)
            schedule = Schedule({}, timestamp=1000)
            schedule.record_outcome('source', Outcome('cleanup_failed', 'local'), timestamp=1000)
            state.patch_monitor(schedule=schedule.record)
            browser = Mock()
            browser.check_session.side_effect = BrowserOutcome(Outcome('remote_rate_limited', 'remote', 'valid', 3600))
            self.assertFalse(ScheduledCalls(root, browser, clock=lambda: 1900).recover_cleanup())
            saved = state.monitor()['schedule']
            self.assertTrue(saved['review_block'])
            self.assertEqual(Schedule(saved, timestamp=1900).next_due, 5500)

    def test_restart_resumes_interrupted_link_without_reconstructing_prior_pages(self):
        from test_pipeline import MemoryDownloader
        from booksys_core.parsing import DEFAULT_CATEGORY
        source = 'https://www7.bookys-ebooks.com/bandes-dessinees/10-new'
        old = 'https://www7.bookys-ebooks.com/bandes-dessinees/9-old'
        link = source + '/dl/1'
        calls = []
        now = [1000]
        busy_once = [True]
        def fetch(url):
            calls.append(url)
            if url == DEFAULT_CATEGORY:
                return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{source}">New</a><a class="bys-item" href="{old}">Old</a></div>'
            if url == source:
                return '<h1>Album</h1><a href="/bandes-dessinees/10-new/dl/1">fixture</a>'
            if url == link:
                if busy_once[0]:
                    busy_once[0] = False
                    raise BrowserOutcome(Outcome('browser_busy', 'local'))
                return '<h1>Link</h1><a class="bys-link bys-host" href="https://host.test/file">Download</a>'
            self.fail('Unexpected reconstruction')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_store(self,root,empty())
            seed_monitor(root, {'version': 1, 'categories': {DEFAULT_CATEGORY: {'after_id': 9}}, 'submissions': {}})
            client = MemoryDownloader()
            with self.assertRaises(Deferred):
                run_once(root, fetch, client, minimum_free=0, clock=lambda: now[0])
            now[0] = 1060
            result = run_once(root, fetch, client, minimum_free=0, clock=lambda: now[0])
            self.assertEqual(calls, [DEFAULT_CATEGORY, source, link, link])
            self.assertEqual(result['queued'], 1)
            self.assertEqual(client.creates, 1)

    def test_stale_submission_save_cannot_erase_new_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_store(self,root,empty())
            state=CollectorState(root)
            stale=state.monitor()
            state.patch_monitor(schedule={'not_before':'retained'})
            stale['submissions']['source']={'status':'queued','package_id':1}
            state.submission('source',stale['submissions']['source'])
            self.assertEqual(state.monitor()['schedule'],{'not_before':'retained'})

    def test_busy_deferral_preserves_schedule_releases_lock_and_sends_no_early_read(self):
        calls = []
        def busy(url):
            calls.append(url)
            raise BrowserOutcome(Outcome('browser_busy', 'local'))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_store(self,root,empty())
            with self.assertRaises(Deferred):
                run_once(root, busy, object(), minimum_free=0)
            with lock(root / 'pipeline.lock', blocking=False):
                pass
            saved = CollectorState(root).monitor()
            self.assertEqual(saved['schedule']['wait_reason'], 'browser_busy')
            with self.assertRaises(Deferred):
                run_once(root, busy, object(), minimum_free=0)
            self.assertEqual(len(calls), 1)

    def test_bounded_steps_retain_watermark_until_all_new_sources_are_submitted(self):
        from test_pipeline import MemoryDownloader
        from booksys_core.parsing import DEFAULT_CATEGORY
        sources = ['https://www7.bookys-ebooks.com/bandes-dessinees/'+str(n)+'-new' for n in (12,11,10)]
        calls = []
        def fetch(url):
            calls.append(url)
            if url == DEFAULT_CATEGORY:
                return '<h1>Catalogue</h1><div class="bys-items-container">'+''.join('<a class="bys-item" href="'+source+'">Book</a>' for source in sources)+'</div>'
            if '/dl/' in url:
                return '<h1>Link</h1><a class="bys-link bys-host" href="https://host.test/file">Get</a>'
            return '<h1>Book</h1><a href="'+url+'/dl/1">Get</a>'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            create_store(self,root,empty())
            seed_monitor(root,{'version':1,'categories':{DEFAULT_CATEGORY:{'after_id':10}},'submissions':{}})
            client=MemoryDownloader()
            first=run_once(root,fetch,client,minimum_free=0,step_limit=1)
            self.assertTrue(first['continued'])
            self.assertEqual(client.creates,1)
            self.assertEqual(CollectorState(root).monitor()['categories'][DEFAULT_CATEGORY]['after_id'],10)
            run_once(root,fetch,client,minimum_free=0,step_limit=1)
            self.assertEqual(client.creates,2)
            self.assertEqual(CollectorState(root).monitor()['categories'][DEFAULT_CATEGORY]['after_id'],12)
            self.assertEqual(calls.count(DEFAULT_CATEGORY),1)
