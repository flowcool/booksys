from postgres_fixture import create_catalog
"""Interactive proof submits work while the normal OCR actor owns its lock."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from library.catalog import Catalog, empty
from library.manual_reader import submit_page, execute_page
from library.primitives import atomic_yaml
from library.stage_actor import StageActor
from library.stage_resources import Resources, Demand
from library.stage_tasks import StageTasks


class ManualProofTests(unittest.TestCase):
    def test_live_ocr_owner_executes_explicit_request_without_lock_handover(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            from postgres_fixture import create_store
            store=create_store(self,root,empty())
            StageTasks.upgrade(store)
            tasks = StageTasks(store)
            resources = Resources.create(root/'resources.yaml')
            catalog = create_catalog(self,root)
            results, errors = [], []
            def request():
                try:
                    results.append(submit_page(catalog, 'a'*64, 1, timeout=5))
                except BaseException as exc:
                    errors.append(exc)
            resolution = SimpleNamespace(_page=lambda asset, page: {'id': 'b'*64, 'asset': asset, 'page': page})
            with StageActor(tasks, resources, 'ocr',
                    lambda task, stamp: execute_page(catalog, task, resolution),
                    demand=lambda task: Demand(memory_mib=1792, cpu_millis=1250, disk=1, output_bytes=4*1024**3),
                    cleanup=lambda task: True) as actor:
                thread = threading.Thread(target=request)
                thread.start()
                until = time.monotonic()+5
                while thread.is_alive() and time.monotonic() < until:
                    actor.step(now=datetime.now(timezone.utc), free_bytes=100*1024**3, external_bytes=0)
                    time.sleep(.01)
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results[0]['id'], 'b'*64)
            self.assertEqual(resources.grants(), [])
