"""Reader selection through admission and the catalogue presentation."""
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from library.admission import AdmissionPublisher
from library.cli import parser, run
from library.intake import receive
from library.primitives import LibraryError
from library.projection import operator_row
from library.readers import sha256
from test_library_intake import SOURCE
from test_library_publish import setup_library
from test_library_readers import image_bytes, rar_bytes


class ReaderSelectionTests(unittest.TestCase):
    def copies(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        store, reception, _, key = setup_library(root, self)
        return store, reception, key, AdmissionPublisher(store, reception, root / 'library', isolated=True)

    def add_copy(self, store, reception, index, pages, extension='png'):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.comment = f'container-{index}'.encode()
            for position, page in enumerate(pages, 1):
                archive.writestr(f'{position:03}.{extension}', page)
        return self.add_file(store, reception, index, 'cbz', data.getvalue())

    def add_file(self, store, reception, index, suffix, data):
        name = f'album-{index}.{suffix}'
        path = reception / 'package' / name
        path.write_bytes(data)
        state = receive(store, reception, {'items': {SOURCE: {'title': 'Album'}}},
            {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}},
            [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                {'fid': index, 'name': name, 'status': 0, 'size': path.stat().st_size}]}],
            stable_seconds=.01)
        store.save(state, expected=state['revision'])
        return sha256(path)

    def identify(self, store, key, work, reference=None, edition=None):
        state = store.working_set('admit', {'assets': [key]})
        asset = state['assets'][key]
        asset['work_result'] = {'status': 'identified', 'work': work}
        if reference:
            asset['work_result']['concordant_candidates'] = [
                {'source': 'bedetheque', 'edition_ref': reference}]
        if edition:
            asset['edition'] = edition
        store.save(state, expected=state['revision'])

    def test_equal_pages_keep_the_exposed_reader_copy_and_both_origins(self):
        store, reception, first, publisher = self.copies()
        second = self.add_copy(store, reception, 2, [image_bytes()])
        work = {'series': 'Messalina', 'number': 5, 'title': 'Le cinquième tome'}
        self.identify(store, first, work, '240926')
        self.identify(store, second, work, '240926')
        exposed = publisher.publish_asset(first)
        self.assertNotEqual(first, second)
        publisher.publish_asset(second)
        publisher.publish_asset(second)

        entry = store.get('assets', second)
        self.assertNotIn('admission', entry)
        self.assertEqual(entry['reader_selection']['duplicate_of'], first)
        self.assertEqual(len(entry['origins']), 1)
        self.assertEqual(list(publisher.root.glob('classified/**/book.cbz')),
                         [publisher.root / exposed['target'] / 'book.cbz'])
        oid, = entry['origins']
        row = operator_row(oid, store.get('origins', oid), entry, {})
        self.assertEqual(row['publication'], 'duplicate')
        self.assertIn(first, row['reason'])

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('unrar', 'pdfimages', 'pdftoppm')),
                         'Native comic and PDF tools required')
    def test_gaston_cbr_pdf_cbz_and_pax_pdf_cbz_use_ordered_output(self):
        store, reception, _, publisher = self.copies()
        data = io.BytesIO()
        Image.new('RGB', (20, 20), 'green').save(data, format='PDF')
        pdf = self.add_file(store, reception, 2, 'pdf', data.getvalue())
        gaston = {'series': 'Gaston (2018)', 'number': 23, 'title': 'Des boum et des paf'}
        self.identify(store, pdf, gaston, '543574')
        self.assertFalse(store.get('assets', pdf).get('page_manifest'))
        first = publisher.publish_asset(pdf)
        with zipfile.ZipFile(publisher.root / first['target'] / 'book.cbz') as archive:
            name = first['transformation']['page_manifest'][0]['name']
            page = archive.read(name)
        ext = name.rsplit('.', 1)[1]
        cbr = self.add_file(store, reception, 3, 'cbr', rar_bytes('01.' + ext, page))
        cbz = self.add_copy(store, reception, 4, [page], ext)
        for asset in (cbr, cbz):
            self.identify(store, asset, gaston, '543574')
            self.assertEqual(publisher.publish_asset(asset)['duplicate_of'], pdf)

        pax = {'series': 'Pax Elfica', 'number': 3, 'title': 'L’épée de Bren'}
        archive = self.add_copy(store, reception, 5, [page], ext)
        pdf_path = reception / 'package' / 'album-2.pdf'
        alternate = self.add_file(store, reception, 6, 'pdf', pdf_path.read_bytes() + b'\n')
        self.identify(store, archive, pax, '539084')
        self.identify(store, alternate, pax, '539084')
        publisher.publish_asset(archive)
        self.assertEqual(publisher.publish_asset(alternate)['duplicate_of'], archive)

    @unittest.skipUnless(shutil.which('unrar'), 'Native comic tool required')
    def test_messalina_t04_cbr_and_cbz_match_across_family_paths(self):
        store, reception, first, publisher = self.copies()
        second = self.add_file(store, reception, 2, 'cbr', rar_bytes('1.png', image_bytes()))
        work = {'series': 'Messalina', 'number': 4, 'title': 'Le quatrième tome'}
        self.identify(store, first, {**work, 'family': 'BD'}, '190492')
        self.identify(store, second, work, '190492')
        publisher.publish_asset(first)
        self.assertEqual(publisher.publish_asset(second)['duplicate_of'], first)

    def test_investigation_review_cases_and_proven_editions_stay_visible(self):
        blank = image_bytes()
        black = io.BytesIO()
        Image.new('RGB', (8, 8), 'black').save(black, format='PNG')
        different = black.getvalue()
        fixtures = (
            ('Druuna Genesis', 'Druuna au commencement', 2, 'Genesis', '486511', '486511',
             [blank, different], [blank], 'pages_differ'),
            ('Centaurus without comparable evidence', 'Centaurus', None, 'Centaurus', '459319', '459319',
             [blank], [blank], 'pages_unavailable'),
            ('Hot moms', 'Hot moms', None, 'Hot moms INT', '443857', '443857',
             [blank], [different], 'pages_differ'),
            ('Druuna Creatura', 'Druuna', 3, 'Creatura', '300', '300',
             [blank, different], [different, blank], 'pages_differ'),
            ('Emmanuelle', 'Emmanuelle', 1, 'Emmanuelle', '301', '302',
             [blank], [different], 'work_unconfirmed'),
            ('Emmanuelle equal pages with disjoint references', 'Emmanuelle', 1, 'Emmanuelle', '301', '302',
             [blank], [blank], 'work_unconfirmed'),
            ('Proven distinct editions', 'Double édition', 1, 'Le même ouvrage', '303', '303',
             [blank], [blank], 'distinct_edition'),
        )
        store, reception, _, publisher = self.copies()
        for i, (case, series, number, title, ref_a, ref_b, one, two, reason) in enumerate(fixtures, 1):
            with self.subTest(case=case):
                first = self.add_copy(store, reception, i * 2, one)
                second = self.add_copy(store, reception, i * 2 + 1, two)
                work = {'series': series, 'number': number, 'title': title}
                edition = reason == 'distinct_edition'
                if case.startswith('Emmanuelle equal'):
                    # An independent cohort with the same identification conflict.
                    work['series'] = 'Emmanuelle synthetic second cohort'
                    work['title'] = 'Emmanuelle synthetic second cohort'
                    ref_a, ref_b = '401', '402'
                self.identify(store, first, work, ref_a, 'e1' if edition else None)
                self.identify(store, second, work, ref_b, 'e2' if edition else None)
                publisher.publish_asset(first)
                if reason == 'pages_unavailable':
                    state = store.working_set('admit', {'assets': [first]})
                    state['assets'][first]['admission']['transformation'].pop('page_manifest')
                    store.save(state, expected=state['revision'])
                published = publisher.publish_asset(second)
                self.assertTrue((publisher.root / published['target'] / 'book.cbz').is_file())
                entry = store.get('assets', second)
                self.assertEqual(entry['reader_selection']['review'], reason)
                oid, = entry['origins']
                row = operator_row(oid, store.get('origins', oid), entry, {})
                self.assertTrue(row['reason'])
                self.assertEqual(row['publication'], 'classified')

    def test_double_seven_stays_in_holding_without_confirmed_identity(self):
        store, reception, first, publisher = self.copies()
        for key in (first, self.add_copy(store, reception, 2, [image_bytes()]),
                    self.add_copy(store, reception, 3, [image_bytes(), image_bytes()])):
            self.assertEqual(publisher.publish_asset(key)['status'], 'holding')
            self.assertNotIn('reader_selection', store.get('assets', key))

    def test_existing_holding_exposure_is_retained_when_identity_arrives(self):
        store, reception, first, publisher = self.copies()
        second = self.add_copy(store, reception, 2, [image_bytes()])
        work = {'series': 'Messalina', 'number': 5, 'title': 'Le cinquième tome'}
        self.identify(store, first, work, '240926')
        publisher.publish_asset(first)
        holding = publisher.publish_asset(second)
        self.identify(store, second, work, '240926')
        self.assertEqual(publisher.publish_asset(second)['duplicate_of'], first)
        self.assertTrue((publisher.root / holding['target'] / 'book.cbz').exists())
        self.assertEqual(len(list(publisher.root.glob('classified/**/book.cbz'))), 1)

    def test_selection_cli_defaults_to_read_only_and_recovers_both_directions(self):
        store, reception, first, publisher = self.copies()
        second = self.add_copy(store, reception, 2, [image_bytes()])
        third = self.add_copy(store, reception, 3, [image_bytes()])
        work = {'series': 'Gaston', 'number': 23, 'title': 'Des boum et des paf'}
        published = {}
        for position, key in enumerate((first, second, third), 1):
            self.identify(store, key, {**work, 'number': position})
            published[key] = publisher.publish_asset(key)
        for key in (first, second, third):
            self.identify(store, key, work, '543574')
        command = ['--state', str(store.directory), 'reader-selection', '--isolated',
                   '--library', str(publisher.root), '--reception', str(reception)]
        def cli(*flags):
            return run(parser().parse_args([*command, *flags]))
        revision = store.revision
        groups = cli()['groups']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['keep'], first)
        self.assertEqual({row['asset'] for row in groups[0]['eligible_to_retain']}, {second, third})
        preview = cli('--select', first, second)
        self.assertEqual(preview['status'], 'dry-run')
        self.assertEqual(store.revision, revision)
        self.assertFalse((publisher.root / '.recovery' / 'reader-selection').exists())
        # Inject interruption after the atomic move and before catalogue commit.
        with patch('library.catalog.Catalog.save', side_effect=LibraryError('interrupted')):
            with self.assertRaisesRegex(LibraryError, 'interrupted'):
                cli('--select', first, second, '--apply')
        self.assertFalse(Path(preview['source']).exists())
        self.assertTrue(Path(preview['target']).exists())
        self.assertEqual(cli('--select', first, second, '--apply')['status'], 'retained')
        self.assertEqual(cli('--select', first, second, '--apply')['status'], 'retained')
        self.assertEqual(store.get('assets', second)['reader_selection']['duplicate_of'], first)
        with self.assertRaisesRegex(LibraryError, 'serves as the default'):
            cli('--select', third, first, '--apply')
        journal = preview['journal']
        self.assertEqual(cli('--restore', journal)['status'], 'dry-run')
        with patch('library.catalog.Catalog.save', side_effect=LibraryError('interrupted')):
            with self.assertRaisesRegex(LibraryError, 'interrupted'):
                cli('--restore', journal, '--apply')
        self.assertEqual(cli('--restore', journal, '--apply')['status'], 'restored')
        self.assertEqual(cli('--restore', journal, '--apply')['status'], 'restored')
        self.assertEqual(store.get('assets', second)['admission']['operation'], published[second]['id'])
        publisher.reconcile()
        self.assertEqual(len(list(publisher.root.glob('classified/**/book.cbz'))), 3)
        self.assertTrue((Path(preview['target']).parent / 'selection.yaml').is_file())
