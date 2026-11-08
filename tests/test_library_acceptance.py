"""Isolated cross-component acceptance; no live state, hoster or pyLoad access."""
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from library.catalog import Catalog, LibraryError
from library.metadata import Providers, add_assertions, enrich
from library.publish import Publisher, Capacity
from library.readers import sha256
from pipeline import run_once
from test_library_publish import setup_library, page_bytes
from test_pipeline import MemoryDownloader, FIRST


def inventory(root):
    return {str(path.relative_to(root)): sha256(path) for path in root.rglob('*') if path.is_file()}


class AcceptanceTests(unittest.TestCase):
    def test_process_death_during_replacement_and_withdrawal(self):
        for kind in ('replace', 'withdraw'):
            for phase in ('intent', 'after_substitution', 'published', 'committed'):
                with self.subTest(kind=kind, phase=phase), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    store, reception, original, asset = setup_library(root)
                    before = inventory(reception)
                    publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
                    initial = publisher.publish(asset)
                    state = store.read()
                    state['assets'][asset]['assertions'] = [{'new': 'revision'}]
                    store.save(state, expected=state['revision'])
                    def run():
                        def crash(point, operation):
                            if point == phase:
                                os._exit(88)
                        worker = Publisher(store, reception, root / 'library', isolated=True,
                                           capacity=Capacity(reserve=0), fault=crash)
                        if kind == 'replace':
                            worker.publish(asset, replace=True)
                        else:
                            worker.withdraw('edition', expected=store.read()['revision'])
                    child = multiprocessing.get_context('fork').Process(target=run)
                    child.start()
                    child.join(10)
                    self.assertEqual(child.exitcode, 88)
                    publisher.reconcile()
                    target = root / 'library/bd' / initial['target']
                    if target.exists():
                        self.assertEqual(page_bytes(target / 'book.cbz'), page_bytes(original))
                        self.assertTrue((target / 'fiche.md').exists())
                    self.assertEqual(inventory(reception), before)

    def test_actual_process_death_preserves_originals_and_reconciles(self):
        for phase in ('intent', 'transforming', 'transformed', 'copying', 'prepared', 'verified', 'after_substitution', 'published', 'committed'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                store, reception, original, asset = setup_library(root)
                before = inventory(reception)
                def run():
                    def crash(point, operation):
                        if point == phase:
                            os._exit(87)
                    Publisher(store, reception, root / 'library', isolated=True,
                        capacity=Capacity(reserve=0), fault=crash).publish(asset)
                child = multiprocessing.get_context('fork').Process(target=run)
                child.start()
                child.join(10)
                self.assertEqual(child.exitcode, 87)
                publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
                publisher.reconcile()
                publisher.publish(asset)
                self.assertEqual(len(list((root / 'library/bd').iterdir())), 1)
                self.assertEqual(inventory(reception), before)

    def test_slow_provider_does_not_block_real_collector_cycle_or_metadata_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root)
            before = inventory(reception)
            entered, release = threading.Event(), threading.Event()
            def slow(url):
                entered.set()
                release.wait(5)
                return 503, ''
            providers = Providers(root / 'cache', fetch=slow, interval=0)
            errors = []
            def run_enrichment():
                try:
                    enrich(store, asset, providers, [('bdtheque', '27089/le-gang-de-la-clef-a-molette')])
                except LibraryError as exc:
                    errors.append(str(exc))
            thread = threading.Thread(target=run_enrichment)
            thread.start()
            self.assertTrue(entered.wait(30 if os.environ.get('BOOKSYS_READER_IMAGE') else 2))
            downloader = MemoryDownloader()
            def fetch(url):
                return '<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="' + FIRST + '">Old</a></div>'
            start = time.monotonic()
            result = run_once(store.directory, fetch, downloader, minimum_free=0)
            self.assertTrue(result['initialized'])
            state = store.read()
            store.save(state, expected=state['revision'])
            self.assertLess(time.monotonic() - start, 2)
            release.set()
            thread.join(30 if os.environ.get('BOOKSYS_READER_IMAGE') else 5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ['Stale catalogue revision'])
            self.assertEqual(inventory(reception), before)

    def test_capacity_envelope_and_one_active_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root)
            with self.assertRaisesRegex(LibraryError, 'not qualified'):
                Capacity(reserve=20 * 1024**3, external_active=lambda: True).check(root, 1)
            free = [20 * 1024**3 + 128 * 1024**2]
            capacity = Capacity(external_active=lambda: True, max_external_bps=64 * 1024**2,
                                qualified=True, max_interval=1, free=lambda _: free[0])
            capacity.check(root, 1024**2)
            free[0] -= 80 * 1024**2
            with self.assertRaisesRegex(LibraryError, 'reserve'):
                capacity.check(root, 1024**2)
            self.assertGreater(free[0], 20 * 1024**3)
            entered, release = threading.Event(), threading.Event()
            def pause(point, operation):
                if point == 'copying':
                    entered.set()
                    release.wait(3)
            publisher = Publisher(store, reception, root / 'library', isolated=True,
                                  capacity=Capacity(reserve=0), fault=pause)
            thread = threading.Thread(target=lambda: publisher.publish(asset))
            thread.start()
            try:
                self.assertTrue(entered.wait(30 if os.environ.get('BOOKSYS_READER_IMAGE') else 2))
                with self.assertRaisesRegex(LibraryError, 'active'):
                    Publisher(store, reception, root / 'library', isolated=True,
                              capacity=Capacity(reserve=0)).publish(asset)
            finally:
                release.set()
                thread.join(30 if os.environ.get('BOOKSYS_READER_IMAGE') else 5)
            self.assertFalse(thread.is_alive())

    def test_manual_override_survives_reenrichment_and_failure_is_not_safe(self):
        asset = {'overrides': {'genres': ['Adventure']}, 'assertions': []}
        add_assertions(asset, {'status': 'ok', 'source': 'bdtheque', 'upstream': 'bdtheque',
            'url': 'https://www.bdtheque.com/series/1/a', 'retrieved_at': '2026-11-08',
            'fields': {'genres': ['Western'], 'public': 'Adultes'}})
        add_assertions(asset, {'status': 'unavailable', 'reason': '503'})
        self.assertEqual(asset['overrides']['genres'], ['Adventure'])
        public = [a for a in asset['assertions'] if a['field'] == 'public']
        self.assertEqual([a['normalized'] for a in public], ['adult'])
        self.assertNotIn('erotica', [str(a['normalized']) for a in asset['assertions']])
