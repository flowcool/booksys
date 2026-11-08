"""The cycle routes all queued sources against one catalogue snapshot."""
import argparse
import contextlib
import importlib.util
import io
import tempfile
import unittest
import sys
import types
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch
from library.catalog import atomic_yaml
from test_library_publish import setup_library


class RoutingSnapshotTests(unittest.TestCase):
    def test_pending_saved_queue_does_not_probe_every_package(self):
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
            provisional = types.SimpleNamespace(process_provisional=lambda *a, **kw: {'processed': 0, 'ready_backlog': 0})
            with patch.dict(sys.modules, {'library.provisional': provisional}), patch('library.automatic.ready_assets', return_value=0, create=True), patch('library.intake.receive'), patch('library.automatic.process_assets', return_value={}), \
                 patch('mirrors.public_hosts', return_value={}), patch('mirror_routing.route_package', side_effect=route), \
                 patch('pipeline.Pyload'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.cycle(args), 0)
            self.assertEqual(seen, [])

    def test_intake_backlog_prevents_ocr_and_conversion_in_real_cycle(self):
        spec = importlib.util.spec_from_file_location('drain_cycle_test', 'ops/library_cycle.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);store, reception, _, _ = setup_library(root)
            current = store.read();origin = next(iter(current['origins'].values()))
            origin.update(availability='pending', reason='Intake budget deferred')
            store.save(current, expected=current['revision'])
            atomic_yaml(root/'captured.yaml', {'items': {}})
            atomic_yaml(root/'submissions.yaml', {'submissions': {}})
            atomic_yaml(root/'queue.yaml', [])
            args = argparse.Namespace(root=root, reception=reception, captured=root/'captured.yaml',
                submissions=root/'submissions.yaml', queue=root/'queue.yaml', credentials=None,
                endpoint='http://127.0.0.1:1', auto=True, budget=0, asset=[], printed=False, route_mirrors=False)
            calls = []
            def provisional(*args, **kwargs):
                calls.append(kwargs['limit'])
                return {'processed': 0, 'ready_backlog': 0}
            with patch.dict(sys.modules, {'library.provisional': types.SimpleNamespace(process_provisional=provisional)}), \
                 patch('library.automatic.ready_assets', return_value=2, create=True), \
                 patch('library.automatic.process_assets') as metadata, patch('library.intake.receive'), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.cycle(args), 0)
            metadata.assert_not_called()
            self.assertEqual(calls, [0])
            result = module.read_yaml(root/'evidence/cycle.yaml')
            self.assertEqual(result['intake_backlog'], 1)
            self.assertEqual(result['heavy_turn'], 0)

    def test_optional_public_read_429_preserves_completed_cycle_and_records_source(self):
        spec = importlib.util.spec_from_file_location('routing_http_cycle_test', 'ops/library_cycle.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, reception, _, _ = setup_library(root)
            source = 'https://bookys-ebooks.com/comics/fixture'
            atomic_yaml(root/'captured.yaml', {'items': {source: {'title': 'Fixture'}}})
            atomic_yaml(root/'submissions.yaml', {'submissions': {source: {'status': 'queued', 'package_id': 7}}})
            atomic_yaml(root/'queue.yaml', [])
            (root/'credentials.json').write_text('{"api_key":"fixture"}')
            args = argparse.Namespace(root=root, reception=reception, captured=root/'captured.yaml',
                submissions=root/'submissions.yaml', queue=root/'queue.yaml', credentials=root/'credentials.json',
                endpoint='http://127.0.0.1:1', auto=True, budget=0, asset=[], printed=False, route_mirrors=True)
            provisional = types.SimpleNamespace(process_provisional=lambda *a, **kw: {'processed': 0, 'ready_backlog': 0})
            with patch.dict(sys.modules, {'library.provisional': provisional}), \
                 patch('library.automatic.ready_assets', return_value=0, create=True), \
                 patch('library.intake.receive'), patch('library.automatic.process_assets', return_value={}), \
                 patch.object(module, 'routing_candidates', return_value=[source]), \
                 patch('mirrors.public_hosts', side_effect=HTTPError('https://api.alldebrid.com/v4/hosts', 429, 'Too many', {}, None)), \
                 patch('mirror_routing.route_package') as route, patch('pipeline.Pyload'), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.cycle(args), 0)
            route.assert_not_called()
            result = module.read_yaml(root/'evidence/cycle.yaml')
            self.assertEqual(result['status'], 'lab')
            self.assertEqual(result['optional_errors'][0]['source'], 'alldebrid_public_hosts')
            self.assertEqual(result['optional_errors'][0]['http_status'], 429)
