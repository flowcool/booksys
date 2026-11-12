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
