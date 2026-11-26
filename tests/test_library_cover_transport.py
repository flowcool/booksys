"""Cover transport policy with an in-memory shared admission authority."""
from contextlib import contextmanager, closing
from copy import deepcopy
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch

from booksys_core.network_admission import RollingBudget, BudgetWait
from library.metadata import Providers
from library.primitives import LibraryError
from library.cover_transport import fetch_binary, main, MAX_BYTES


class MemoryBudget(RollingBudget):
    def __init__(self, state):
        self.state = state
        self.path = Path('/unused/provider-admission.yaml')

    @contextmanager
    def _transaction(self, *, write=False):
        yield self.state


class CoverTransportTests(unittest.TestCase):
    def provider(self, policy='User-agent: *\nAllow: /'):
        clock = SimpleNamespace(value=100., sleeps=[])
        def sleep(seconds):
            clock.sleeps.append(seconds)
            clock.value += seconds
        self.enterContext(patch('library.metadata.time', SimpleNamespace(
            time=lambda: clock.value, monotonic=lambda: clock.value, sleep=sleep)))
        self.enterContext(patch('booksys_core.network_admission.time.time', lambda: clock.value))
        self.enterContext(patch('library.dependency_recovery.DependencyRecovery'))
        state = dict(schema=1, requests=50, period=300, spacing=1, review_block=False,
                     last=0., not_before=0., starts=[])
        provider = object.__new__(Providers)
        provider.shared = None
        provider.interval, provider.budget, provider.last_request = 1, 50, 0
        provider.admission = MemoryBudget(state)
        provider.events = []
        cache, calls = {}, []
        provider._cache = SimpleNamespace(get=cache.get, retain=lambda k,v: cache.update({k:v}))
        provider.fetch = lambda url: (calls.append(url) or (200, policy))
        provider.binary_fetch = lambda url: (calls.append(url) or (200, b'private-binary'))
        return provider, clock, state, cache, calls

    def test_shared_pacing_cache_namespace_and_restart(self):
        provider, clock, state, cache, calls = self.provider()
        cover = 'https://www.bedetheque.com/media/Couvertures/123.jpg'
        provider.get('https://www.bedetheque.com/BD-Example-123.html')
        self.assertEqual(provider.get_binary(cover)['status'], 'ok')
        self.assertEqual(len(state['starts']), 3)
        for a, b in zip(state['starts'], state['starts'][1:]):
            self.assertGreaterEqual(b - a + 1e-9, 6.3)
        before = deepcopy(state)
        provider.get_binary(cover)
        self.assertEqual(state, before)
        self.assertIn('cover-binary-v1:' + cover, cache)
        self.assertNotIn(cover, cache)
        restarted = object.__new__(Providers)
        restarted.__dict__.update(provider.__dict__)
        restarted.last_request = 0
        restarted.get('https://www.bedetheque.com/BD-Next-124.html')
        self.assertGreaterEqual(state['starts'][-1] - state['starts'][-2] + 1e-9, 6.3)
        self.assertEqual(state['spacing'], 1)  # No live policy rewrite.

    def test_robots_is_required_even_for_a_cached_cover(self):
        for policy in ('', 'User-agent: *\nDisallow: /media/'):
            with self.subTest(policy=policy):
                provider, clock, state, cache, calls = self.provider(policy)
                url = 'https://www.bedetheque.com/media/123.jpg'
                cache['cover-binary-v1:' + url] = {'status': 'ok', 'body': b'old', 'expires': 10000}
                self.assertNotEqual(provider.get_binary(url)['status'], 'ok')
                self.assertEqual(len(calls), 1)

    def test_window_and_retry_after_are_not_short_pacing_waits(self):
        provider, clock, state, cache, calls = self.provider()
        state.update(not_before=200)
        self.assertEqual(provider.get_binary('https://www.bedetheque.com/media/123.jpg')['status'], 'unavailable')
        self.assertEqual(calls, [])
        self.assertEqual(clock.sleeps, [])
        state.update(not_before=0, requests=1)
        provider.get('https://www.bedetheque.com/robots.txt')
        self.assertEqual(provider.get_binary('https://www.bedetheque.com/media/123.jpg')['status'], 'unavailable')
        self.assertEqual(len(state['starts']), 1)

    def test_cover_origin_is_checked_before_robots_or_fetch(self):
        provider, clock, state, cache, calls = self.provider()
        for url in ('http://www.bedetheque.com/a.jpg', 'https://cdn.bedetheque.com/a.jpg',
                    'https://user@www.bedetheque.com/a.jpg', 'https://www.bedetheque.com:444/a.jpg',
                    'https://www.bedetheque.com/a.jpg#fragment'):
            with self.subTest(url=url), self.assertRaises(LibraryError):
                provider.get_binary(url)
        self.assertEqual(calls, [])

    def test_absolute_deadline_kills_transport_with_one_attempt(self):
        with patch('library.cover_transport.subprocess.run', side_effect=subprocess.TimeoutExpired('cover', 20)) as run:
            with self.assertRaises(LibraryError):
                fetch_binary('https://www.bedetheque.com/media/123.jpg')
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs['timeout'], 20)

    def test_child_rejects_html_redirect_and_oversized_response(self):
        url = 'https://www.bedetheque.com/media/123.jpg'
        for content_type, body, final_url in (
            ('text/html', b'<html>challenge</html>', url),
            ('image/jpeg', b'<html>challenge</html>', url),
            ('image/jpeg', b'\xff\xd8\xff' + b'x'*MAX_BYTES, url),
            ('image/jpeg', b'\xff\xd8\xffx', 'https://cdn.example/a.jpg')):
            response = BytesIO(body)
            response.headers = Message()
            response.headers['Content-Type'] = content_type
            response.geturl = lambda: final_url
            response.status = 200
            opener = SimpleNamespace(open=lambda *a, **kw: closing(response))
            with patch('library.cover_transport.sys.argv', ['transport', url]), \
                    patch('library.cover_transport.build_opener', return_value=opener), \
                    patch('builtins.print') as output:
                main()
                self.assertIn('reason', output.call_args.args[0])

    def test_schema_two_codec_retains_bytes_and_additive_receipts(self):
        from library.codec import encode, decode
        value = {'body': b'\x00\xff', 'work_result': {'evidence': {
            'cover_confirmation': {'score': 40.0, 'result': 'pass'}}}}
        self.assertEqual(decode(encode(value)), value)
