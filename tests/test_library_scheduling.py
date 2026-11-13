import unittest

class SchedulingTests(unittest.TestCase):


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
