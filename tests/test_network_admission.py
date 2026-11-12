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

    def test_uncertain_native_write_blocks_every_producer_until_exact_owner_readback(self):
        from booksys_core.downloader import Pyload
        from booksys_core.errors import CollectionError
        budget = RollingBudget.create(self.root/'native-admission.yaml', requests=30, period=60)
        first = Pyload('http://127.0.0.1:8000', 'fixture', admission=budget)
        second = Pyload('http://127.0.0.1:8000', 'fixture', admission=RollingBudget(budget.path))
        journal = self.root/'native-source.yaml'
        journal.write_text('intent: retained\n')
        with patch.object(first, '_call', side_effect=OSError('uncertain')):
            with self.assertRaises(OSError):
                with first.mutation('owner-a', journal):
                    first.call('move_package', write=True, package_id=1)
        with patch.object(second, '_call', return_value={}) as send:
            with self.assertRaises(CollectionError):
                with second.mutation('owner-b', journal):
                    second.call('delete_packages', write=True, package_ids=[2])
            self.assertEqual(send.call_count, 0)
            # Exact owner's readback completes its domain journal without resend.
            with second.mutation('owner-a', journal) as mutation:
                second.call('get_package_data', package_id=1)
                mutation.resolve('exact-domain-receipt')
            with second.mutation('owner-b', journal) as mutation:
                second.call('move_package', write=True, package_id=2)
                mutation.resolve('second-exact-receipt')
            self.assertEqual(send.call_count, 2)

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

    def test_real_submission_reconciles_uncertain_create_before_next_write_without_resend(self):
        from booksys_core.downloader import Pyload
        from test_pipeline import MemoryDownloader, NEW
        native=MemoryDownloader()
        native.lose_response=True
        budget=RollingBudget.create(self.root/'native-admission.yaml',requests=30,period=60)
        record={'title':'Fixture','downloads':{'one':{'hoster':'fixture','urls':['https://host.test/file'],'metadata':'CBZ'}}}
        journal={'submissions':{}}
        path=self.root/'monitor.yaml'
        first=Pyload('http://127.0.0.1:8000','fixture',admission=budget)
        with patch.object(first,'_call',side_effect=native.call),patch.object(first,'host_availability',side_effect=native.host_availability):
            with self.assertRaises(OSError):
                first.submit(NEW,record,journal,path)
        second=Pyload('http://127.0.0.1:8000','fixture',admission=RollingBudget(budget.path))
        with patch.object(second,'_call',side_effect=native.call),patch.object(second,'host_availability',side_effect=native.host_availability):
            self.assertEqual(second.submit(NEW,record,journal,path),1)
        self.assertEqual(native.creates,1)
        self.assertEqual(journal['submissions'][NEW]['status'],'queued')

    def test_real_retirement_uses_the_same_native_budget_and_exact_receipt(self):
        from booksys_core.downloader import Pyload
        from library.retirement import Retirement
        from test_retirement import fixture
        from test_library_intake import SOURCE
        store,reception,collector,original,native=fixture(self.root/'retirement',self)
        scripts=self.root/'scripts'
        scripts.mkdir()
        budget=RollingBudget.create(self.root/'native-admission.yaml',requests=30,period=60)
        client=Pyload('http://127.0.0.1:8000','fixture',admission=budget)
        with patch.object(client,'_call',side_effect=native.call):
            result=Retirement(client,store,reception,collector/'monitor.yaml',collector/'state.yaml',scripts=scripts).process(SOURCE,apply=True)
        self.assertEqual(result['outcome'],'retired')
        self.assertEqual(native.removed,[1])
        self.assertTrue(original.is_file())

    def test_unknown_prepare_commit_prevents_send_and_requires_same_owner_journal(self):
        from booksys_core.downloader import Pyload
        from booksys_core.network_admission import NativeMutation
        from booksys_core.errors import CollectionError
        from library.postgres import DeadlineConnection,CommitUnknown
        import psycopg
        budget=RollingBudget.create(self.root/'native-admission.yaml',requests=30,period=60)
        client=Pyload('http://127.0.0.1:8000','fixture',admission=budget)
        journal=self.root/'domain-proof.yaml'
        original=DeadlineConnection.execute
        armed=False
        def lost_ack(connection,query,params=None,**kwargs):
            nonlocal armed
            result=original(connection,query,params,**kwargs)
            if isinstance(query,str) and query.startswith('UPDATE native_effect'):
                armed=True
            elif query=='COMMIT' and armed:
                armed=False
                raise psycopg.OperationalError('commit acknowledgement lost')
            return result
        with patch.object(client,'_call',return_value={}) as send:
            with patch.object(DeadlineConnection,'execute',lost_ack):
                with self.assertRaises(CommitUnknown):
                    with client.mutation('owner-a',journal):
                        client.call('move_package',write=True,package_id=1)
            self.assertEqual(send.call_count,0)
            self.assertEqual(NativeMutation(self.root,'owner-a',journal).read()['state'],'uncertain')
            with client.mutation('owner-a',self.root/'wrong-journal.yaml') as wrong:
                with self.assertRaises(CollectionError):
                    wrong.resolve('wrong-domain-proof')
            with client.mutation('owner-a',journal) as exact:
                client.call('get_package_data',package_id=1)
                exact.resolve('exact-readback-no-send')
            self.assertEqual(send.call_count,1)
