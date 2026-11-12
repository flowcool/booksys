"""Paused candidate resource replacement, with isolated retained records."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import json
from postgres_fixture import create_store
from library.primitives import empty
import unittest
from types import SimpleNamespace

from library.primitives import LibraryError
from library.stage_model import Obligation, Task
from library.stage_resources import Demand, Resources
from ops.stage_lab import requalify_native_memory

class StageLabTests(unittest.TestCase):
    def test_upgrade_keeps_grants_waiter_age_and_identity_and_admits_again(self):
        age = datetime(2026, 11, 9, tzinfo=timezone.utc)
        obligation = Obligation('routing', 'source', 'revision', 'method')
        task = Task(obligation.key, 'routing', 'source', 'revision', 'method', age.isoformat(), age.isoformat())
        tasks = SimpleNamespace(get=lambda key: task if key == task.key else None)
        with tempfile.TemporaryDirectory() as directory:
            store=create_store(self,Path(directory),empty())
            resources = Resources.create(Path(directory)/'resources.yaml')
            old = Demand(memory_mib=128, cpu_millis=250, native=1)
            self.assertEqual(resources.admit('holder', age=age, demand=old, free_bytes=100*1024**3, external_bytes=0), '')
            key = task.key+':1'
            self.assertEqual(resources.admit(key, age=age, demand=old, free_bytes=100*1024**3, external_bytes=0), 'resource_wait')
            with resources._transaction() as current:
                value=deepcopy(current)
            # Use the same real task identity for an interrupted grant as well.
            value['grants'][task.key+':0'] = value['grants'].pop('holder')
            before = deepcopy(value)
            upgraded = requalify_native_memory(value, tasks)
            self.assertEqual(value, before)
            for table in ('grants', 'waiters'):
                self.assertEqual(upgraded[table].keys(), before[table].keys())
                for record in upgraded[table].values():
                    self.assertEqual(record['age'], age.isoformat())
                    self.assertEqual(record['demand'], asdict(Demand(memory_mib=512, cpu_millis=250, native=1)))
            # Install the deliberately retained upgraded fixture in the sole
            # authority. The retired YAML path cannot change resource ownership.
            with store._database.connection(write=True) as db:
                db.execute('UPDATE resource_pools SET capacity=%s WHERE pool=%s',
                           (json.dumps(upgraded['capacity']),resources.pool))
                for table in ('grants','waiters'):
                    db.execute(f'DELETE FROM resource_{table} WHERE pool=%s',(resources.pool,))
                    for retained_key,record in upgraded[table].items():
                        db.execute(f'INSERT INTO resource_{table} VALUES (%s,%s,%s,%s,%s)',
                            (resources.pool,retained_key,record['age'],record.get('queue',''),json.dumps(record['demand'])))
            resources.release(task.key+':0', stopped=True, children_stopped=True)
            self.assertEqual(resources.admit(key, age=age, demand=Demand(memory_mib=512,cpu_millis=250,native=1), free_bytes=100*1024**3, external_bytes=0), '')

    def test_upgrade_refuses_a_retained_pool_that_cannot_hold_new_grants(self):
        task = SimpleNamespace(stage='routing')
        tasks = SimpleNamespace(get=lambda key: task if key == 'native' else None)
        value = {'capacity': {'memory_mib':2048}, 'waiters': {}, 'grants': {
            'native:1': {'age':'retained', 'demand':asdict(Demand(memory_mib=128,cpu_millis=250,native=1))},
            'heavy:1': {'age':'retained', 'demand':asdict(Demand(memory_mib=1792,cpu_millis=1250,disk=1))}}}
        before = deepcopy(value)
        with self.assertRaisesRegex(LibraryError, 'replacement task pool'):
            requalify_native_memory(value, tasks)
        self.assertEqual(value, before)
