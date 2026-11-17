"""Durable pre-send admission through actual network producer interfaces."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from booksys_core.network_admission import RollingBudget, BudgetWait
from library.metadata import Providers

class NetworkAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from tests.postgres_fixture import create_store
        from library.primitives import empty
        self.store=create_store(self,self.root,empty())
        self.path = self.root / 'provider-admission.yaml'
        self.budget = RollingBudget.create(self.path, requests=2, period=300, spacing=1)

    def test_uncertain_send_is_charged_restart_and_backward_clock_keep_debits(self):
        self.budget.debit(timestamp=1000)
        with self.assertRaises(BudgetWait) as caught:
            RollingBudget(self.path).debit(timestamp=1000.5)
        self.assertEqual(caught.exception.due, 1001)
        self.budget.debit(timestamp=1001)
        with self.assertRaises(BudgetWait) as caught:
            RollingBudget(self.path).debit(timestamp=999)
        self.assertEqual(caught.exception.due, 1001)
        with self.assertRaises(BudgetWait) as caught:
            RollingBudget(self.path).debit(timestamp=1002)
        self.assertEqual(caught.exception.due, 1300)
        RollingBudget(self.path).debit(timestamp=1300)
        with self.assertRaises(BudgetWait):
            RollingBudget(self.path).debit(timestamp=1300.5)

    def test_every_provider_instance_shares_debit_and_cache_does_not_charge(self):
        sent = []
        def fetch(url):
            sent.append(url)
            if len(sent) == 1:
                raise OSError('uncertain send')
            return 200, '{}'
        with patch('time.time', return_value=1000):
            first = Providers(self.root/'cache', shared=self.root, fetch=fetch, interval=0)
            self.assertEqual(first.get('https://openlibrary.org/books/OL1M.json')['status'], 'unavailable')
            second = Providers(self.root/'cache', shared=self.root, fetch=fetch, interval=0)
            self.assertEqual(second.get('https://openlibrary.org/books/OL2M.json')['reason'], 'Request budget exhausted')
        with patch('time.time', return_value=1001):
            self.assertEqual(second.get('https://openlibrary.org/books/OL2M.json')['status'], 'ok')
            self.assertEqual(second.get('https://openlibrary.org/books/OL2M.json')['status'], 'ok')
        self.assertEqual(len(sent), 2)
        with self.assertRaises(BudgetWait):
            RollingBudget(self.path).debit(timestamp=1002)

    def test_native_transport_debits_before_failure_for_reads_and_writes(self):
        from booksys_core.downloader import Pyload
        budget = RollingBudget.create(self.root/'native-admission.yaml', requests=1, period=60)
        client = Pyload('http://127.0.0.1:8000', 'fixture', admission=budget)
        with patch.object(client, '_call', side_effect=OSError('uncertain')) as send:
            with self.assertRaises(OSError):
                with client.mutation('transport', self.root/'source.yaml'):
                    client.call('move_package', write=True, package_id=1)
            with self.assertRaises(BudgetWait):
                client.call('get_queue')
            self.assertEqual(send.call_count, 1)


    def test_readiness_does_not_spend_a_debit_and_missing_authority_fails_closed(self):
        from booksys_core.errors import CollectionError
        self.budget.debit(timestamp=1000)
        self.assertEqual(self.budget.next_due(timestamp=1000.5), 1001)
        self.assertEqual(RollingBudget(self.path).next_due(timestamp=1000.5), 1001)
        with patch.dict('os.environ', {'BOOKSYS_ADMISSION_STATE': str(self.root)}):
            provider = Providers(self.root/'missing-cache', fetch=lambda url: (200,'{}'), interval=0)
            with self.store._database.connection(write=True) as db:
                db.execute('DELETE FROM network_starts WHERE scope=%s',(self.path.name,))
                db.execute('DELETE FROM network_budgets WHERE scope=%s',(self.path.name,))
            result = provider.get('https://openlibrary.org/books/OL1M.json')
            self.assertEqual(result['status'], 'unavailable')
            self.assertIn('authority unavailable', result['reason'])

    def test_native_remote_retry_after_is_retained_across_client_reconstruction(self):
        from booksys_core.downloader import Pyload
        from urllib.error import HTTPError
        budget=RollingBudget.create(self.root/'native-admission.yaml',requests=30,period=60)
        first=Pyload('http://127.0.0.1:8000','fixture',admission=budget)
        error=HTTPError('http://127.0.0.1:8000/api/get_queue',429,'Busy',{'Retry-After':'600'},None)
        with patch('time.time',return_value=1000),patch.object(first,'_call',side_effect=error):
            with self.assertRaises(HTTPError):
                first.call('get_queue')
        second=Pyload('http://127.0.0.1:8000','fixture',admission=RollingBudget(budget.path))
        with patch('time.time',return_value=1100),patch.object(second,'_call',return_value=[]) as send:
            with self.assertRaises(BudgetWait):
                second.call('get_queue')
            self.assertEqual(send.call_count,0)

    def test_provider_remote_deadline_blocks_a_different_url_and_new_producer(self):
        from urllib.error import HTTPError
        error=HTTPError('https://openlibrary.org/books/OL1M.json',429,'Busy',{'Retry-After':'600'},None)
        with patch.dict('os.environ',{'BOOKSYS_ADMISSION_STATE':str(self.root)}),patch('time.time',return_value=1000),patch('library.metadata.build_opener') as opener:
            opener.return_value.open.side_effect=error
            first=Providers(self.root/'http-cache',interval=0)
            self.assertEqual(first.get('https://openlibrary.org/books/OL1M.json')['status'],'unavailable')
        with patch.dict('os.environ',{'BOOKSYS_ADMISSION_STATE':str(self.root)}),patch('time.time',return_value=1100):
            sent=[]
            second=Providers(self.root/'second-cache',fetch=lambda url:sent.append(url) or (200,'{}'),interval=0)
            self.assertEqual(second.get('https://openlibrary.org/books/OL2M.json')['status'],'unavailable')
            self.assertEqual(sent,[])




class PackageNameTests(unittest.TestCase):
    def test_package_name_preserves_199_and_200_bytes(self):
        from booksys_core.downloader import _package_name
        marker = 'Booksys-0123456789abcdef'
        for size in (199, 200):
            title = 'A' * (size - len(marker) - 2) + ' '
            expected = marker + ' ' + title
            with self.subTest(size=size):
                self.assertEqual(_package_name(marker, title).encode('utf-8'), expected.encode('utf-8'))

    def test_package_name_bounds_ascii_and_strips_trailing_space(self):
        from booksys_core.downloader import _package_name
        marker = 'Booksys-0123456789abcdef'
        name = _package_name(marker, 'A' * 174 + ' ' + 'B' * 225)
        self.assertLessEqual(len(name.encode('utf-8')), 200)
        self.assertTrue(name.startswith(marker + ' '))
        self.assertFalse(name.endswith(' '))

    def test_package_name_preserves_utf8_character_boundaries(self):
        from booksys_core.downloader import _package_name
        name = _package_name('Booksys-0123456789abcdef', '\u00e9' * 300)
        self.assertLessEqual(len(name.encode('utf-8')), 200)
        self.assertEqual(name.encode('utf-8').decode('utf-8'), name)
        self.assertEqual(name[25:], '\u00e9' * 87)

    def test_package_name_keeps_distinct_source_markers(self):
        import hashlib
        from booksys_core.downloader import _package_name
        sources = ('https://example.invalid/first', 'https://example.invalid/second')
        markers = ['Booksys-' + hashlib.sha256(source.encode()).hexdigest()[:16] for source in sources]
        self.assertNotEqual(_package_name(markers[0], 'A' * 400 + 'first'),
                            _package_name(markers[1], 'A' * 400 + 'second'))
