import unittest
from library.scheduling import next_delay, heavy_stage

class SchedulingTests(unittest.TestCase):
    def test_finished_intake_drains_without_idle_or_heavy_work(self):
        self.assertEqual(next_delay({"status": "lab", "intake_backlog": 70}), 0)
        self.assertIsNone(heavy_stage(70, 4))
        self.assertEqual([heavy_stage(0, n) for n in range(6)],
                         ["provisional", "provisional", "metadata"] * 2)
        self.assertEqual(next_delay({"status": "lab", "ready_backlog": 1}), 0)
        self.assertEqual(next_delay({"status": "blocked", "intake_backlog": 70}), 300)
        self.assertEqual(next_delay({"status": "lab", "backlog": 100}), 300)

    def test_drain_model_has_no_ocr_cost_or_idle_between_ready_bursts(self):
        # A 96-file fixture at 2 seconds per validation. The former 8-file
        # cycle also paid one 180-second OCR plus 300 idle seconds per burst.
        clock = 0
        remaining = 96
        turns = 0
        while remaining:
            consumed = min(32, remaining)
            remaining -= consumed
            clock += consumed * 2
            self.assertIsNone(heavy_stage(remaining or 1, turns))
            clock += next_delay({'status': 'lab', 'intake_backlog': remaining or 1})
            turns += 1
        self.assertEqual((turns, clock), (3, 192))
        self.assertEqual(heavy_stage(0, 2), 'metadata')

    def test_saved_queue_candidate_scan_needs_no_live_package_reads(self):
        from library.scheduling import routing_candidates
        source = 'https://bookys-ebooks.com/comics/fixture'
        entry = {'status': 'queued', 'package_id': 7, 'primary_links': ['https://host/one']}
        queue = [{'pid': 7, 'links': [{'fid': 4, 'url': 'https://host/one', 'status': 0}]}]
        state = {'origins': {'o': {'source': source, 'pid': 7, 'fid': 4, 'availability': 'validated'}}}
        self.assertEqual(routing_candidates(queue, {source: entry}, state, {}), [source])
        queue[0]['links'][0]['status'] = 12
        self.assertEqual(routing_candidates(queue, {source: entry}, state, {}), [])
        journal = {'packages': {'7': {'phase': 'stopped', 'kind': 'park'}}}
        self.assertEqual(routing_candidates(queue, {source: entry}, state, journal), [])

    def test_routing_read_failure_does_not_hide_prior_write_attempt(self):
        from library.scheduling import RoutingClient
        from urllib.error import HTTPError
        class API:
            def call(self, method, **kwargs):
                raise HTTPError('http://localhost/api/' + method, 429, 'fixture', {}, None)
        client = RoutingClient(API())
        with self.assertRaises(HTTPError):
            client.call('get_package_data')
        self.assertFalse(client.wrote)
        self.assertEqual(client.endpoint, '/api/get_package_data')
        with self.assertRaises(HTTPError):
            client.call('move_package', write=True)
        self.assertTrue(client.wrote)
