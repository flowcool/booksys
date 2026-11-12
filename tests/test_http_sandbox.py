"""Qualify inherited listening/IPC sockets in the actual installed bubblewrap."""
from pathlib import Path
import socket
import json
import urllib.error
import tempfile
import time
import unittest
import urllib.request
from library.http_runtime import HTTPRuntime


class FakeAPI:
    host = ''
    def invoke(self, action, request):
        if action == 'status':
            return {'schema': 1, 'host': request['host']}
        raise ValueError('Unsupported')


class SandboxTests(unittest.TestCase):
    def test_restricted_routes_reject_invalid_host_and_duplicate_json_fields(self):
        runtime = HTTPRuntime(('127.0.0.1', 0), FakeAPI())
        try:
            runtime.start()
            base = 'http://127.0.0.1:' + str(runtime.address[1])
            request = urllib.request.Request(base + '/', headers={'Host': 'untrusted.invalid'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + '/api/resolution/confirm',
                data=b'{"preview":"one","preview":"two"}', headers={'Content-Type': 'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 400)
        finally:
            runtime.close()

    def test_actual_child_serves_through_ipc_with_no_host_state_mount(self):
        runtime = HTTPRuntime(('127.0.0.1', 0), FakeAPI())
        try:
            runtime.start()
            base = 'http://127.0.0.1:' + str(runtime.address[1])
            for attempt in range(20):
                try:
                    with urllib.request.urlopen(base + '/api/status', timeout=1) as response:
                        self.assertEqual(json.loads(response.read())['schema'], 1)
                    break
                except (OSError, TimeoutError):
                    if attempt == 19:
                        raise
                    time.sleep(.05)
            self.assertIsNone(runtime.child.poll())
            # Read mountinfo of the sandboxed Python, through the bwrap child tree.
            parent = runtime.child.pid
            descendants = Path('/proc/' + str(parent) + '/task/' + str(parent) + '/children').read_text().split()
            self.assertTrue(descendants)
            maps = Path('/proc/' + descendants[0] + '/mountinfo').read_text()
            self.assertNotIn(' /data/booksys ', maps)
            self.assertNotIn('docker.sock', maps)
        finally:
            runtime.close()
