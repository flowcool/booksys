"""Framed IPC preserves request ownership and separate bounded execution lanes."""
import socket
import threading
import time
import unittest
from library.ipc import Channel, Controller, Remote, IPCError


class IPCTests(unittest.TestCase):
    def test_compressed_compatibility_refuses_expansion_past_complete_body_bound(self):
        import base64, zlib
        first, second = socket.socketpair()
        server = Channel(first)
        def respond():
            request = server.receive()
            server.send({'id': request['id'], 'error': None, 'result': {
                '_compatibility_zlib': base64.b64encode(zlib.compress(b'x' * (16 * 1024**2 + 1))).decode()}})
        thread = threading.Thread(target=respond, daemon=True); thread.start()
        remote = Remote(Channel(second))
        try:
            with self.assertRaises(IPCError):
                remote.call('status', {})
        finally:
            remote.close(); server.close(); thread.join(1)

    def test_slow_proof_does_not_hold_reads_and_disconnect_does_not_resend(self):
        first, second = socket.socketpair()
        entered = threading.Event()
        calls = []
        def invoke(action, value):
            calls.append(action)
            if action == 'page':
                entered.set(); time.sleep(.3)
            return {'action': action}
        controller = Controller(Channel(first), invoke)
        thread = threading.Thread(target=controller.serve, daemon=True);thread.start()
        remote = Remote(Channel(second))
        slow = threading.Thread(target=lambda: remote.call('page', {'asset': 'a', 'page': 1}))
        slow.start(); self.assertTrue(entered.wait(1))
        start = time.monotonic()
        self.assertEqual(remote.call('status', {}), {'action': 'status'})
        self.assertLess(time.monotonic() - start, .2)
        slow.join(2)
        remote.close(); controller.close(); thread.join(1)
        self.assertEqual(calls.count('page'), 1)

    def test_oversized_malformed_frame_and_unknown_action_fail_closed(self):
        first, second = socket.socketpair()
        channel = Channel(first)
        second.sendall((4194305).to_bytes(4, 'big'))
        with self.assertRaises(IPCError):
            channel.receive()
        first.close(); second.close()
        first, second = socket.socketpair()
        controller = Controller(Channel(first), lambda action, value: self.fail('No dispatch'))
        thread = threading.Thread(target=controller.serve, daemon=True);thread.start()
        remote = Remote(Channel(second))
        with self.assertRaises(IPCError):
            remote.call('run_command', {'command': 'docker'})
        remote.close(); controller.close(); thread.join(1)

    def test_compatibility_chunks_and_proof_binary_segment_are_bounded(self):
        first, second = socket.socketpair()
        value = {'rows': ['x' * 1000] * 5000}
        import base64
        png = b'png' * 1000000
        def invoke(action, payload):
            return value if action in ('status', 'activity') else {'png': base64.b64encode(png).decode()}
        controller = Controller(Channel(first), invoke)
        thread = threading.Thread(target=controller.serve, daemon=True); thread.start()
        remote = Remote(Channel(second))
        try:
            self.assertEqual(remote.call('status', {}), value)
            self.assertEqual(remote.call('activity', {}), value)
            self.assertEqual(base64.b64decode(remote.call('proof', {})['png']), png)
        finally:
            remote.close(); controller.close(); thread.join(1)

    def test_identical_inflight_diagnostic_searches_share_one_job(self):
        from concurrent.futures import ThreadPoolExecutor
        first, second = socket.socketpair()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def invoke(action, value):
            calls.append(action); entered.set(); release.wait(1)
            return {'state': 'complete', 'events': []}
        controller = Controller(Channel(first), invoke)
        thread = threading.Thread(target=controller.serve, daemon=True); thread.start()
        remote = Remote(Channel(second))
        try:
            with ThreadPoolExecutor(max_workers=4) as workers:
                jobs = [workers.submit(remote.call, 'diagnostics', {'query': 'same'}) for _ in range(4)]
                self.assertTrue(entered.wait(1)); time.sleep(.05); release.set()
                self.assertTrue(all(job.result()['state'] == 'complete' for job in jobs))
            self.assertEqual(calls, ['diagnostics'])
        finally:
            release.set(); remote.close(); controller.close(); thread.join(1)

    def test_identical_concurrent_status_views_share_one_snapshot(self):
        from concurrent.futures import ThreadPoolExecutor
        first, second = socket.socketpair()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def invoke(action, value):
            calls.append(action); entered.set(); release.wait(1)
            return {'rows': [], 'schema': 1}
        controller = Controller(Channel(first), invoke)
        thread = threading.Thread(target=controller.serve, daemon=True); thread.start()
        remote = Remote(Channel(second))
        try:
            with ThreadPoolExecutor(max_workers=4) as workers:
                jobs = [workers.submit(remote.call, 'status', {}) for _ in range(4)]
                self.assertTrue(entered.wait(1)); time.sleep(.05); release.set()
                self.assertTrue(all(job.result()['schema'] == 1 for job in jobs))
            self.assertEqual(calls, ['status'])
        finally:
            release.set(); remote.close(); controller.close(); thread.join(1)
