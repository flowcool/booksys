"""The cycle routes all queued sources against one catalogue snapshot."""
import argparse
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from library.catalog import atomic_yaml
from test_library_publish import setup_library


class RoutingSnapshotTests(unittest.TestCase):
    def test_queued_routing_reuses_one_snapshot_for_all_sources(self):
        spec = importlib.util.spec_from_file_location('routing_cycle_test', 'ops/library_cycle.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, reception, _, _ = setup_library(root)
            sources = ['https://bookys.invalid/source-a', 'https://bookys.invalid/source-b']
            atomic_yaml(root / 'captured.yaml', {'items': {s: {'title': 'Fixture'} for s in sources}})
            atomic_yaml(root / 'submissions.yaml', {'submissions': {s: {'status': 'queued', 'package_id': i}
                                                                   for i, s in enumerate(sources)}})
            atomic_yaml(root / 'queue.yaml', [])
            # Credentials are JSON and belong solely to this temporary fixture.
            (root / 'credentials.json').write_text('{"api_key":"fixture"}')
            args = argparse.Namespace(root=root, reception=reception, captured=root / 'captured.yaml',
                submissions=root / 'submissions.yaml', queue=root / 'queue.yaml', credentials=root / 'credentials.json',
                endpoint='http://127.0.0.1:1', auto=True, budget=0, asset=[], printed=False, route_mirrors=True)
            seen = []
            def route(client, source, entry, record, catalogue, *rest, **kwargs):
                seen.append((source, catalogue))
                return {'outcome': 'nothing'}
            with patch('library.intake.receive'), patch('library.automatic.process_assets', return_value={}), \
                 patch('mirrors.public_hosts', return_value={}), patch('mirror_routing.route_package', side_effect=route), \
                 patch('pipeline.Pyload'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.cycle(args), 0)
            self.assertEqual([entry[0] for entry in seen], sources)
            self.assertIs(seen[0][1], seen[1][1])
            self.assertIn('assets', seen[0][1])
