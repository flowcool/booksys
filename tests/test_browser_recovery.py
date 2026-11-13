"""Qualified local browser recovery never probes a hoster or creates a package."""
import io
import unittest
from unittest.mock import patch
from booksys_core.browser import Byparr
from booksys_core.errors import BrowserError


class BrowserRecoveryTests(unittest.TestCase):

    def test_unhealthy_browser_sends_no_source_request(self):
        browser = Byparr('http://byparr:8191/v1', delay=0)
        with patch('booksys_core.browser.urlopen', return_value=io.BytesIO(b'{"status":"failed"}')), \
                patch.object(browser, 'fetch_html') as fetch:
            with self.assertRaises(BrowserError): browser.check_session()
            fetch.assert_not_called()
