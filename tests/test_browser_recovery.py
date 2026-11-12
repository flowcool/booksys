"""Qualified local browser recovery never probes a hoster or creates a package."""
import io
import unittest
from unittest.mock import patch
from booksys_core.browser import Byparr
from booksys_core.errors import BrowserError


class BrowserRecoveryTests(unittest.TestCase):
    def test_health_and_valid_catalogue_are_both_required(self):
        health = b'{"status":"ok","generation":1,"requests":3}'
        for html, expected in (('<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="/bandes-dessinees/1-album">Album</a></div>', True),
                               ('<h1>Unexpected page</h1>', False)):
            with self.subTest(expected=expected):
                browser = Byparr('http://byparr:8191/v1', delay=0)
                with patch('booksys_core.browser.urlopen', side_effect=lambda *a, **k: io.BytesIO(health)) as requests, \
                        patch.object(browser, 'fetch_html', return_value=html) as fetch:
                    if expected:
                        self.assertTrue(browser.check_session())
                        self.assertEqual(requests.call_count, 2)
                    else:
                        with self.assertRaises(BrowserError): browser.check_session()
                    self.assertEqual(fetch.call_count, 1)

    def test_unhealthy_browser_sends_no_source_request(self):
        browser = Byparr('http://byparr:8191/v1', delay=0)
        with patch('booksys_core.browser.urlopen', return_value=io.BytesIO(b'{"status":"failed"}')), \
                patch.object(browser, 'fetch_html') as fetch:
            with self.assertRaises(BrowserError): browser.check_session()
            fetch.assert_not_called()
