"""Durable retry allowance and remote deadlines at the agreed scheduler seam."""
import unittest

from booksys_core.scheduler import Schedule, Deferred
from booksys_core.browser_outcomes import Outcome, retry_after


class ScheduleTests(unittest.TestCase):
    def test_two_busy_retries_shared_across_urls_and_restart(self):
        schedule = Schedule({}, timestamp=1000)
        schedule.before('first', timestamp=1000)
        schedule.record_outcome('first', Outcome('browser_busy', 'local'), timestamp=1000)
        with self.assertRaises(Deferred):
            schedule.before('first', timestamp=1059)
        schedule = Schedule(schedule.record, timestamp=1060)
        schedule.before('first', timestamp=1060)
        self.assertEqual(schedule.record['busy_used'], 1)
        schedule.success('first')
        schedule.before('second', timestamp=1061)
        schedule.record_outcome('second', Outcome('browser_busy', 'local'), timestamp=1061)
        schedule.before('second', timestamp=1121)
        self.assertEqual(schedule.record['busy_used'], 2)
        schedule.record_outcome('second', Outcome('browser_busy', 'local'), timestamp=1121)
        self.assertEqual(schedule.next_due, 2021)
        with self.assertRaises(Deferred):
            Schedule(schedule.record, timestamp=1900).before('third', timestamp=1900)

    def test_remote_review_boundaries_and_expiry_never_release_review(self):
        for seconds, blocked in ((86399, False), (86400, False), (86401, True)):
            schedule = Schedule({}, timestamp=1000)
            schedule.record_outcome('source', Outcome('remote_rate_limited', 'remote', retry_after_state='valid',
                                                     retry_after_seconds=seconds), timestamp=1000)
            self.assertEqual(schedule.record['review_block'], blocked)
            with self.assertRaises(Deferred):
                schedule.before('unrelated', timestamp=1001)
            resumed = Schedule(schedule.record, timestamp=1000 + seconds + 1)
            if blocked:
                with self.assertRaises(Deferred):
                    resumed.before('source', timestamp=1000 + seconds + 1)
            else:
                resumed.before('source', timestamp=1000 + seconds + 1)

    def test_retry_header_overflow_and_dates_are_explicit(self):
        self.assertEqual(retry_after('60', received_at=0), ('valid', 60))
        self.assertEqual(retry_after('Thu, 01 Jan 1970 00:00:01 GMT', received_at=10), ('valid', 0))
        self.assertEqual(retry_after('9' * 1000, received_at=0), ('unrepresentable', None))
        self.assertEqual(retry_after('60, 90', received_at=0), ('invalid', None))
        self.assertEqual(retry_after(None, received_at=0), ('absent', None))

    def test_reconstruction_defers_before_sending_and_does_not_renew_a_busy_chain(self):
        schedule = Schedule({}, timestamp=1000)
        original = schedule.record['cycle_id']
        schedule.record_outcome('item', Outcome('browser_busy', 'local'), timestamp=1890)
        schedule.before('item', timestamp=1950)
        self.assertEqual(schedule.record['cycle_id'], original)
        schedule.record_outcome('item', Outcome('browser_busy', 'local'), timestamp=1950)
        with self.assertRaises(Deferred):
            schedule.before('catalogue', timestamp=2010, reconstruction_calls=2)
        self.assertEqual(schedule.next_due, 2910)
        self.assertIsNone(schedule.record['request_ref'])
        schedule.before('catalogue', timestamp=2910)
        self.assertNotEqual(schedule.record['cycle_id'], original)

    def test_repeated_invalid_reference_keeps_its_count_across_episodes(self):
        schedule = Schedule({}, timestamp=1000)
        schedule.record_outcome('bad', Outcome('invalid_content', 'remote'), timestamp=1000)
        schedule.before('bad', timestamp=1900)
        schedule.record_outcome('bad', Outcome('invalid_content', 'remote'), timestamp=1900)
        self.assertEqual(schedule.record['repeat_count'], 2)

    def test_reconstruction_consumes_every_call_and_rejects_corrupt_schedule(self):
        schedule = Schedule({}, timestamp=1000)
        schedule.record_outcome('item', Outcome('browser_busy', 'local'), timestamp=1000)
        schedule.before('item', timestamp=1060, reconstruction_calls=1)
        self.assertEqual(schedule.record['busy_used'], 2)
        for field, value in (('review_block', 'false'), ('repeat_count', -1),
                             ('state', 'invented'), ('remote_deadline', 'not a time')):
            invalid = dict(schedule.record, **{field: value})
            with self.assertRaises(Exception):
                Schedule(invalid, timestamp=1060)
        self.assertEqual(retry_after('0' * 5000, received_at=0), ('valid', 0))


class CleanupRecoveryEventTests(unittest.TestCase):
    def test_failed_qualification_emits_one_safe_event_with_saved_deadline(self):
        import copy
        import tempfile
        from contextlib import nullcontext
        from pathlib import Path
        from unittest.mock import patch
        from booksys_core.browser_outcomes import BrowserOutcome
        from booksys_core.diagnostics import event
        from booksys_core.errors import BrowserError
        from booksys_core.scheduler import ScheduledCalls

        failures = (
            (BrowserOutcome(Outcome('browser_timeout', 'local')), 'browser_timeout', 2800),
            (BrowserOutcome(Outcome('remote_rate_limited', 'remote', retry_after_state='valid',
                                   retry_after_seconds=1200)), 'remote_rate_limited', 3100),
            (BrowserError('secret=do-not-log'), 'cleanup_failed', 2800),
            ('', 'cleanup_failed', 2800),
        )
        for failure, reason, deadline in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                class Fetch:
                    def __call__(self, url):
                        raise AssertionError('No source request expected')

                    def check_session(self):
                        if isinstance(failure, Exception):
                            raise failure
                        return failure

                schedule = Schedule({}, timestamp=1000)
                schedule.record_outcome('source', Outcome('cleanup_failed', 'local'), timestamp=1000)
                saved = {'version': 1, 'schedule': copy.deepcopy(schedule.record)}

                class State:
                    def __init__(self, directory):
                        pass

                    def monitor(self):
                        return copy.deepcopy(saved)

                    def patch_monitor(self, *, schedule):
                        saved['schedule'] = copy.deepcopy(schedule)

                calls = ScheduledCalls(Path(directory), Fetch(), clock=lambda: 1900)
                with patch('booksys_core.scheduler.CollectorState', State), \
                        patch('booksys_core.network_admission.lane', return_value=nullcontext()), \
                        patch('booksys_core.scheduler.emit') as emitted:
                    self.assertFalse(calls.recover_cleanup())
                    emitted.assert_called_once()
                    fields = emitted.call_args.kwargs
                    self.assertEqual((fields['service'], fields['stage'], fields['code'], fields['reason']),
                                     ('collector', 'byparr', 'degraded', reason))
                    self.assertEqual(fields['next_attempt'], saved['schedule']['not_before'])
                    self.assertEqual(Schedule(saved['schedule'], timestamp=1900).next_due, deadline)
                    self.assertEqual(saved['schedule']['cleanup_recovery']['status'], 'failed')
                    self.assertTrue(saved['schedule']['review_block'])
                    self.assertNotIn('do-not-log', str(event(**fields)))
                    self.assertFalse(calls.recover_cleanup())  # Backoff sends no repeated event/probe.
                    emitted.assert_called_once()
