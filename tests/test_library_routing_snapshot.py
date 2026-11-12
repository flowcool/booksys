"""The cycle routes a bounded fair batch against one catalogue snapshot."""
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
from library.operational_state import CollectorState, RuntimeObservation, StatusObservations
from library.postgres import Database

def seed(root, name, value):
    database=Database.configured(root/'state')
    RuntimeObservation(database).activate({'fixture': True},role='library')
    collector=CollectorState(root)
    if name=='captured':
        for source,record in value['items'].items():collector.capture(source,record)
    elif name=='submissions':
        for source,record in value['submissions'].items():collector.submission(source,record)
    else:
        atomic_yaml(root/'queue.yaml',value)  # Explicit offline ingestion input.
        RuntimeObservation(database).publish(value,at=1,remaining=None,overshoot=None)



class RoutingSnapshotTests(unittest.TestCase):
    def test_pending_saved_queue_does_not_probe_every_package(self):
        spec = importlib.util.spec_from_file_location('routing_cycle_test', 'ops/library_cycle.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, reception, _, _ = setup_library(root,self)
            sources = [f'https://bookys.invalid/source-{i:02}' for i in range(20)]
            seed(root,'captured', {'items': {s: {'title': 'Fixture'} for s in sources}})
            seed(root,'submissions', {'submissions': {s: {'status': 'queued', 'package_id': i}
                                                                   for i, s in enumerate(sources)}})
            seed(root,'queue', [])
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
            with patch('library.admission.process_admissions', side_effect=provisional.process_provisional), patch('library.automatic.ready_assets', return_value=0, create=True), patch('library.intake.receive'), patch('library.automatic.process_assets', return_value={}), \
                 patch('mirrors.public_hosts', return_value={}), patch('mirror_routing.route_package', side_effect=route), \
                 patch('booksys_core.downloader.Pyload'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.cycle(args), 0, StatusObservations(Database.configured(root/'state')).cycle())
            self.assertEqual(seen, [])

    def test_intake_backlog_prevents_ocr_and_conversion_in_real_cycle(self):
        spec = importlib.util.spec_from_file_location('drain_cycle_test', 'ops/library_cycle.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);store, reception, _, _ = setup_library(root,self)
            current = store.read();origin = next(iter(current['origins'].values()))
            origin.update(availability='pending', reason='Intake budget deferred')
            store.save(current, expected=current['revision'])
            seed(root,'captured', {'items': {}})
            seed(root,'submissions', {'submissions': {}})
            seed(root,'queue', [])
            args = argparse.Namespace(root=root, reception=reception, captured=root/'captured.yaml',
                submissions=root/'submissions.yaml', queue=root/'queue.yaml', credentials=None,
                endpoint='http://127.0.0.1:1', auto=True, budget=0, asset=[], printed=False, route_mirrors=False)
            calls = []
            def provisional(*args, **kwargs):
                calls.append(kwargs['limit'])
                return {'processed': 0, 'ready_backlog': 0}
            with patch('library.admission.process_admissions', side_effect=provisional), \
                 patch('library.automatic.ready_assets', return_value=2, create=True), \
                 patch('library.automatic.process_assets') as metadata, patch('library.intake.receive'), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.cycle(args), 0, StatusObservations(Database.configured(root/'state')).cycle())
            metadata.assert_not_called()
            self.assertEqual(calls, [0])
            result = StatusObservations(Database.configured(root/'state')).cycle()
            self.assertEqual(result['intake_backlog'], 1)
            self.assertEqual(result['heavy_turn'], 0)

    def test_optional_public_read_429_preserves_completed_cycle_and_records_source(self):
        spec = importlib.util.spec_from_file_location('routing_http_cycle_test', 'ops/library_cycle.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, reception, _, _ = setup_library(root,self)
            source = 'https://bookys-ebooks.com/comics/fixture'
            seed(root,'captured', {'items': {source: {'title': 'Fixture'}}})
            seed(root,'submissions', {'submissions': {source: {'status': 'queued', 'package_id': 7}}})
            seed(root,'queue', [])
            (root/'credentials.json').write_text('{"api_key":"fixture"}')
            args = argparse.Namespace(root=root, reception=reception, captured=root/'captured.yaml',
                submissions=root/'submissions.yaml', queue=root/'queue.yaml', credentials=root/'credentials.json',
                endpoint='http://127.0.0.1:1', auto=True, budget=0, asset=[], printed=False, route_mirrors=True)
            provisional = types.SimpleNamespace(process_provisional=lambda *a, **kw: {'processed': 0, 'ready_backlog': 0})
            with patch('library.admission.process_admissions', side_effect=provisional.process_provisional), \
                 patch('library.automatic.ready_assets', return_value=0, create=True), \
                 patch('library.intake.receive'), patch('library.automatic.process_assets', return_value={}), \
                 patch('library.cycle.routing_candidates', return_value=[source]), \
                 patch('mirrors.public_hosts', side_effect=HTTPError('https://api.alldebrid.com/v4/hosts', 429, 'Too many', {}, None)), \
                 patch('mirror_routing.route_package') as route, patch('booksys_core.downloader.Pyload'), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.cycle(args), 0, StatusObservations(Database.configured(root/'state')).cycle())
            route.assert_not_called()
            result = StatusObservations(Database.configured(root/'state')).cycle()
            self.assertEqual(result['status'], 'lab')
            self.assertEqual(result['optional_errors'][0]['source'], 'alldebrid_public_hosts')
            self.assertEqual(result['optional_errors'][0]['http_status'], 429)
