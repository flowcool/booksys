"""Shared whole-grant admission through the public resource authority."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest

from library.stage_resources import Resources, Demand

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)
GIB = 1024**3

class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'resources.yaml'
        from tests.postgres_fixture import create_store
        from library.primitives import empty
        self.store = create_store(self,self.path.parent,empty())
        self.resources = Resources.create(self.path)

    def admit(self, key, age, demand, **kwargs):
        return self.resources.admit(key, age=age, demand=demand, free_bytes=100*GIB,
                                    external_bytes=0, **kwargs)

    def test_unreserved_policy_keeps_ownership_without_capacity_waits(self):
        from library.primitives import LibraryError
        resources = Resources.create(self.path.parent/'unreserved.yaml',
                                     external_policy='unreserved', reserve_bytes=0)
        demand = Demand(memory_mib=1792, cpu_millis=1250, disk=1, output_bytes=12*GIB)
        for key in ('uncertain:1', 'intake:1', 'ocr:1', 'copies:1'):
            self.assertEqual(resources.admit(key, age=AT, demand=demand,
                free_bytes=0, external_bytes=None, queue='intake'), '')
        resources = Resources(resources.path)
        self.assertEqual(len(resources.grants()), 4)
        self.assertEqual(resources.waiters(), [])
        self.assertEqual(resources.reservation('uncertain:1'), demand)
        with self.assertRaises(LibraryError):
            resources.release('uncertain:1', stopped=True, children_stopped=False)
        with self.assertRaises(LibraryError):
            resources.admit('uncertain:1', age=AT, demand=Demand(),
                            free_bytes=0, external_bytes=None, queue='intake')

    def test_old_large_waiter_reserves_next_release_without_partial_holds(self):
        small = Demand(memory_mib=768, cpu_millis=250)
        heavy = Demand(memory_mib=1792, cpu_millis=1250, disk=1, output_bytes=12*GIB)
        self.assertEqual(self.admit('running', AT, small), '')
        self.assertEqual(self.admit('old', AT+timedelta(seconds=1), heavy), 'resource_wait')
        for index in range(20):
            self.assertEqual(self.admit('new'+str(index), AT+timedelta(seconds=index+2), small), 'older_waiter')
        self.resources = Resources(self.path)
        self.resources.release('running', stopped=True, children_stopped=True)
        self.assertEqual(self.admit('old', AT+timedelta(seconds=1), heavy), '')
        self.assertEqual(self.admit('other-heavy', AT+timedelta(seconds=30), heavy), 'older_waiter')

    def test_unknown_pyload_capacity_defers_output_and_impossible_is_blocked(self):
        heavy = Demand(memory_mib=1792, cpu_millis=1250, disk=1, output_bytes=12*GIB)
        self.assertEqual(self.resources.admit('unknown', age=AT, demand=heavy, free_bytes=100*GIB,
                                             external_bytes=None), 'external_capacity_unknown')
        self.assertEqual(self.admit('metadata', AT, Demand(memory_mib=256, cpu_millis=250)), '')
        self.assertEqual(self.admit('impossible', AT, Demand(memory_mib=2305)), 'blocked_capacity')
        self.assertEqual(self.resources.admit('full', age=AT, demand=heavy, free_bytes=35*GIB,
                                             external_bytes=4*GIB), 'output_capacity')

    def test_lab_unknown_pyload_allows_bounded_work_but_keeps_reserved_headroom(self):
        path = self.path.parent/'lab.yaml'
        Resources.create(path, external_policy='lab-reserve')
        lab = Resources(path)  # Policy survives an actor restart.
        work = Demand(memory_mib=1792, cpu_millis=1250, disk=1, output_bytes=12*GIB)
        self.assertEqual(lab.admit('intake', age=AT, demand=work,
            free_bytes=33*GIB, external_bytes=None), '')
        self.assertEqual(lab.admit('another-output', age=AT, demand=Demand(output_bytes=2*GIB),
            free_bytes=33*GIB, external_bytes=None), 'output_capacity')
        lab.release('intake', stopped=True, children_stopped=True)
        self.assertEqual(lab.admit('too-full', age=AT, demand=work,
            free_bytes=31*GIB, external_bytes=None), 'output_capacity')

    def test_retirement_io_needs_no_external_output_allowance(self):
        self.assertEqual(self.resources.admit('retirement', age=AT,
            demand=Demand(memory_mib=512, cpu_millis=250, disk=1),
            free_bytes=21*GIB, external_bytes=None), '')

    def test_lab_old_output_waiter_still_reserves_next_release(self):
        lab = Resources.create(self.path.parent/'lab.yaml', external_policy='lab-reserve')
        def admit(key, age, demand):
            return lab.admit(key, age=age, demand=demand, free_bytes=100*GIB, external_bytes=None)
        small = Demand(memory_mib=768, cpu_millis=250)
        heavy = Demand(memory_mib=1792, cpu_millis=1250, disk=1, output_bytes=12*GIB)
        self.assertEqual(admit('running', AT, small), '')
        self.assertEqual(admit('old', AT+timedelta(seconds=1), heavy), 'resource_wait')
        self.assertEqual(admit('younger', AT+timedelta(seconds=2), small), 'older_waiter')
        lab.release('running', stopped=True, children_stopped=True)
        self.assertEqual(admit('old', AT+timedelta(seconds=1), heavy), '')

    def test_pruning_does_not_resolve_another_queues_source_journal(self):
        from unittest.mock import patch
        from library.stage_model import Obligation
        from library.stage_tasks import StageTasks
        tasks = StageTasks(self.store)
        owner = tasks.discover(Obligation('collection', 'source', '1', 'v1'), now=AT)
        waiting = owner.key + ':1'
        self.assertEqual(self.admit('running:1', AT, Demand(memory_mib=512)), '')
        self.assertEqual(self.admit(waiting, AT, Demand(memory_mib=2000)), 'resource_wait')
        # A different queue reads ownership, without decoding the task record.
        with patch('library.stage_tasks.Task', side_effect=AssertionError('Unrelated payload decoded')):
            self.resources.prune_waiters(tasks, 'intake', now=AT)
        self.assertEqual(self.resources.waiters(), [waiting])

    def test_pruning_resolves_own_task_without_holding_shared_pool_lock(self):
        from unittest.mock import patch
        from library.postgres import guards
        from library.stage_tasks import StageTasks
        tasks = StageTasks(self.store)
        self.assertEqual(self.admit('running', AT, Demand(memory_mib=512)), '')
        self.assertEqual(self.admit('stale:1', AT, Demand(memory_mib=2000)), 'resource_wait')
        original = tasks._waiter_tasks
        def read(keys, queue):
            with self.store._database.connection(write=True, seconds=.5) as db:
                guards(db, {'resource-pool:'+self.path.name})
            return original(keys, queue)
        with patch.object(tasks, '_waiter_tasks', side_effect=read):
            self.resources.prune_waiters(tasks, 'intake', now=AT)
        self.assertEqual(self.resources.waiters(), [])
        self.assertEqual(self.resources.grants(), ['running'])

    def test_pruning_keeps_a_waiter_refreshed_after_owner_snapshot(self):
        from unittest.mock import patch
        from library.postgres import guards
        from library.stage_tasks import StageTasks
        tasks = StageTasks(self.store)
        self.assertEqual(self.admit('running', AT, Demand(memory_mib=512)), '')
        self.assertEqual(self.admit('stale:1', AT, Demand(memory_mib=2000)), 'resource_wait')
        original = tasks._waiter_tasks
        def read(keys, queue):
            result = original(keys, queue)
            with self.store._database.connection(write=True) as db:
                guards(db, {'resource-pool:'+self.path.name})
                db.execute('UPDATE resource_waiters SET age=%s WHERE pool=%s AND key=%s',
                           ((AT+timedelta(seconds=1)).isoformat(), self.path.name, 'stale:1'))
            return result
        with patch.object(tasks, '_waiter_tasks', side_effect=read):
            self.resources.prune_waiters(tasks, 'intake', now=AT)
        self.assertEqual(self.resources.waiters(), ['stale:1'])

    def test_another_queue_does_not_reserve_idle_capacity(self):
        heavy = Demand(memory_mib=1792, cpu_millis=1250, disk=1)
        self.assertEqual(self.resources.admit('running', age=AT, demand=heavy, free_bytes=100*GIB, external_bytes=0, queue='copies'), '')
        self.assertEqual(self.resources.admit('old-copy', age=AT, demand=heavy, free_bytes=100*GIB, external_bytes=0, queue='copies'), 'resource_wait')
        self.resources.release('running', stopped=True, children_stopped=True)
        self.assertEqual(self.resources.admit('younger-copy', age=AT+timedelta(seconds=1), demand=heavy, free_bytes=100*GIB, external_bytes=0, queue='copies'), 'older_waiter')
        self.assertEqual(self.resources.admit('retire', age=AT+timedelta(seconds=1), demand=Demand(memory_mib=512,disk=1), free_bytes=100*GIB, external_bytes=0, queue='retirement'), '')
        self.assertEqual(self.resources.admit('old-copy', age=AT, demand=heavy, free_bytes=100*GIB, external_bytes=0, queue='copies'), 'resource_wait')
        self.resources.release('retire', stopped=True, children_stopped=True)
        self.assertEqual(self.resources.admit('old-copy', age=AT, demand=heavy, free_bytes=100*GIB, external_bytes=0, queue='copies'), '')

    def test_grants_survive_restart_and_cannot_expire_or_release_live_children(self):
        from library.primitives import LibraryError
        demand = Demand(memory_mib=1792, cpu_millis=1250, disk=1, output_bytes=12*GIB)
        self.assertEqual(self.admit('owner', AT, demand), '')
        self.resources = Resources(self.path)
        with self.assertRaises(LibraryError):
            self.resources.release('owner', stopped=True, children_stopped=False)
        self.assertEqual(self.admit('next', AT+timedelta(days=10), demand), 'resource_wait')
        self.resources.release('owner', stopped=True, children_stopped=True)
        self.assertEqual(self.admit('next', AT+timedelta(days=10), demand), '')

    def test_output_space_wait_does_not_reserve_idle_compute(self):
        heavy = Demand(memory_mib=1792, cpu_millis=1250, disk=1, output_bytes=12*GIB)
        self.assertEqual(self.resources.admit('old', age=AT, demand=heavy,
            free_bytes=25*GIB, external_bytes=0), 'output_capacity')
        self.assertEqual(self.resources.admit('metadata', age=AT+timedelta(seconds=1),
            demand=Demand(memory_mib=256, cpu_millis=250), free_bytes=25*GIB, external_bytes=0), '')

    def test_disk_waiter_reserves_needed_capacity_but_allows_compute_surplus(self):
        self.assertEqual(self.admit('retirement', AT, Demand(memory_mib=256, cpu_millis=250, disk=1)), '')
        heavy = Demand(memory_mib=1792, cpu_millis=1250, disk=1)
        self.assertEqual(self.admit('copies', AT+timedelta(seconds=1), heavy), 'resource_wait')
        self.assertEqual(self.admit('metadata', AT+timedelta(seconds=2), Demand(memory_mib=256, cpu_millis=250)), '')
        self.resources.release('retirement', stopped=True, children_stopped=True)
        self.assertEqual(self.admit('copies', AT+timedelta(seconds=1), heavy), '')

    def test_younger_output_cannot_spend_space_reserved_for_an_admissible_waiter(self):
        self.assertEqual(self.admit('disk-holder', AT, Demand(disk=1)), '')
        self.assertEqual(self.resources.admit('old', age=AT+timedelta(seconds=1), demand=Demand(disk=1,output_bytes=12*GIB),
            free_bytes=33*GIB, external_bytes=0), 'resource_wait')
        self.assertEqual(self.resources.admit('younger', age=AT+timedelta(seconds=2), demand=Demand(output_bytes=2*GIB),
            free_bytes=33*GIB, external_bytes=0), 'older_waiter')
