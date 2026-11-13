import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer

from library.catalog import atomic_yaml
from library.web import build_projection, handler
from test_library_publish import setup_library


class WebTests(unittest.TestCase):
    def test_operator_reason_explains_scheduling_without_changing_backing_reason(self):
        from library.web import operator_reason
        self.assertIn('Téléchargement', operator_reason('Transfer is not confirmed finished'))
