"""The fixed Supervisor group preserves native acting ownership and receipts."""
import configparser
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from library.catalog import empty
from postgres_fixture import create_store
from library.stage_actor import StageActor
from library.stage_model import Obligation
from library.stage_resources import Resources, Demand
from library.stage_runtime import demand
from library.stage_tasks import StageTasks

ROOT=Path(__file__).resolve().parents[1]

def child(root,queue):
    from library.store import Store
    from booksys_core.diagnostics import install_logging
    install_logging('library')
    store=Store(root/'state',authority='processing-proof')
    tasks=StageTasks(store);resources=Resources(root/'state/resources.yaml')
    stop=False
    def finish(*args):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,finish)
    def execute(task,stamp):
        receipt=root/(task.key+'.receipt')
        if receipt.exists():raise AssertionError('Effect resent')
        receipt.write_text(task.key)
        if queue=='metadata' and not (root/'crashed').exists():
            (root/'crashed').write_text(str(os.getpid()))
            time.sleep(1.2) # Enter RUNNING before a real unexpected process death.
            os._exit(23)
        return 'done'
    def reconcile(task):
        return ('done',task.key) if (root/(task.key+'.receipt')).exists() else ('uncertain','')
    (root/(queue+'.pid')).write_text(str(os.getpid()))
    with StageActor(tasks,resources,queue,execute,cleanup=lambda task:True,
                    demand=demand,receipt=reconcile) as actor:
        while not stop:
            outcome=actor.step(now=datetime.now(timezone.utc),free_bytes=100*1024**3,external_bytes=0)
            # Match the worker's idle cadence. Busy-polling a finished sibling
            # creates unrelated PostgreSQL traffic during the recovery proof.
            time.sleep(1 if outcome=='idle' else .01)

class ProcessingTests(unittest.TestCase):
    def test_fixed_map_and_health_require_both_native_roles(self):
        import yaml
        conf=configparser.ConfigParser();conf.read(ROOT/'ops/processing.conf')
        self.assertEqual(set(conf.sections()),{'supervisord','program:ocr','program:metadata'})
        for queue in ('ocr','metadata'):
            section=conf['program:'+queue]
            self.assertIn('library.stage_worker',section['command'])
            self.assertTrue(section.getboolean('killasgroup'))
            self.assertTrue(section.getboolean('stopasgroup'))
            self.assertEqual(section['autorestart'],'unexpected')
            self.assertIn('BOOKSYS_NAS_PROBE_ROLE="'+queue+'"',section['environment'])
        services=yaml.safe_load((ROOT/'compose.stack.yaml').read_text())['services']
        self.assertNotIn('ocr',services);self.assertNotIn('metadata',services)
        self.assertEqual(len([v for v in services.values() if not v.get('profiles')]),11)
        self.assertEqual(services['postgres']['network_mode'], 'none')
        group=services['processing']
        self.assertEqual(group['mem_limit'],'512m');self.assertEqual(group['pids_limit'],96)
        self.assertEqual(group['healthcheck']['test'][-2:],['ocr','metadata'])
        self.assertFalse(any(v['target']=='/var/run/docker.sock' for v in group['volumes']))

    def test_health_rejects_either_stale_child(self):
        from unittest.mock import patch
        from library.stack_health import main
        from library.operational_state import RuntimeObservation
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=create_store(self,root/'state',empty())
            observations=RuntimeObservation(store._database)
            for stale in (None,'ocr','metadata'):
                for role in ('ocr','metadata'):
                    observations.heartbeat(role, at=time.time()-(30 if role==stale else 0))
                with patch.dict(os.environ,{'BOOKSYS_LIBRARY_ROOT':str(root)}),patch.object(sys,'argv',['health','ocr','metadata']):
                    if stale:
                        with self.assertRaises(SystemExit):main()
                    else:main()

    def test_child_restart_keeps_sibling_progress_and_exact_retained_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=create_store(self, root/'state',empty(),authority='processing-proof')
            StageTasks.upgrade(store);tasks=StageTasks(store)
            Resources.create(root/'state/resources.yaml',capacity=Demand(memory_mib=1856,cpu_millis=2000,disk=1,browser=1,native=1))
            stamp=datetime.now(timezone.utc)
            metadata=tasks.discover(Obligation('metadata','confirmed-effect','v1','v1'),now=stamp)
            conf=configparser.ConfigParser();conf.read(ROOT/'ops/processing.conf')
            conf['supervisord']['pidfile']=str(root/'supervisor.pid')
            for queue in ('ocr','metadata'):
                conf['program:'+queue]['command']=f'{sys.executable} {Path(__file__).resolve()} --child {root} {queue}'
                conf['program:'+queue]['directory']=str(ROOT)
                conf['program:'+queue]['environment']='PYTHONPATH="'+str(ROOT)+'"'
                conf['program:'+queue]['startsecs']='1'
            path=root/'supervisor.conf'
            with path.open('w') as stream:conf.write(stream)
            with (root/'stdout').open('wb') as out,(root/'stderr').open('wb') as err:
                process=subprocess.Popen([sys.executable,'-m','supervisor.supervisord','-c',str(path)],stdout=out,stderr=err)
                try:
                    added=[];completed=set();index=0
                    # Test escape bounds, not an aggregate throughput contract.
                    # Abort after three acquisition budgets without completed
                    # work. The outer cap also bounds steady work.
                    deadline=time.monotonic()+360
                    progress_deadline=time.monotonic()+30
                    next_observation=0.0
                    while time.monotonic()<min(deadline,progress_deadline):
                        now=datetime.now(timezone.utc)
                        # Sustained independent intake of queue obligations.
                        if index<30:
                            added.append(tasks.discover(Obligation('ocr','incoming-'+str(index),'v1','v1'),now=now));index+=1
                        if time.monotonic()>=next_observation:
                            next_observation=time.monotonic()+.25
                            for receipt in root.glob('*.receipt'):
                                if receipt.stem in completed:
                                    continue
                                retained=tasks.get(receipt.stem)
                                if retained is not None and retained.status=='done':
                                    completed.add(retained.key)
                                    progress_deadline=time.monotonic()+30
                        if len(added)==30 and len(completed)==31:
                            break
                        time.sleep(.05)
                    self.assertEqual(Resources(root/'state/resources.yaml').capacity().memory_mib,1856)
                    self.assertEqual(demand(tasks.get(metadata.key)).memory_mib,256)
                    self.assertTrue(all(demand(tasks.get(t.key)).memory_mib==1792 for t in added))
                    self.assertEqual(len(added),30)
                    self.assertTrue(all(tasks.get(t.key).status=='done' for t in added),
                        [(tasks.get(t.key).status,tasks.get(t.key).wait_reason,tasks.get(t.key).attempts) for t in added if tasks.get(t.key).status!='done'])
                    self.assertEqual(tasks.get(metadata.key).status,'done')
                    self.assertEqual(tasks.get(metadata.key).attempts,1)
                    self.assertNotEqual((root/'crashed').read_text(),(root/'metadata.pid').read_text())
                    self.assertEqual(len(list(root.glob('*.receipt'))),31)
                    self.assertEqual(Resources(root/'state/resources.yaml').grants(),[])
                    self.assertEqual(tasks.get(metadata.key).first_eligible_at,metadata.first_eligible_at or tasks.get(metadata.key).due_at)
                except BaseException:
                    if target := os.environ.get('BOOKSYS_PROCESSING_PROOF'):
                        evidence = Path(target)
                        (evidence.with_suffix('.stdout')).write_bytes((root/'stdout').read_bytes())
                        (evidence.with_suffix('.stderr')).write_bytes((root/'stderr').read_bytes())
                        resources = Resources(root/'state/resources.yaml')
                        with store._database.connection() as db:
                            states = [dict(key=key,queue=queue,status=status,wait_reason=reason,wait_until=due,
                                           generation=generation,attempts=attempts) for key,queue,status,reason,due,generation,attempts in db.execute(
                                "SELECT key,stage,status,payload::jsonb->>'wait_reason',payload::jsonb->>'wait_until',"
                                "payload::jsonb->>'generation',payload::jsonb->>'attempts' FROM stage_tasks ORDER BY stage,age,key")]
                            waits = [dict(key=key,age=age,queue=queue) for key,age,queue in db.execute(
                                'SELECT key,age,queue FROM resource_waiters ORDER BY age,key')]
                        evidence.write_text(json.dumps({'tasks':states,'waiters':waits,'grants':resources.grants(),
                            'receipts':len(list(root.glob('*.receipt'))),
                            'supervisor_exit':process.poll()},indent=2))
                    raise
                finally:
                    process.terminate();process.wait(timeout=30)
            from booksys_core.diagnostics import parse_event
            events=[parse_event(line) for line in (root/'stderr').read_text().splitlines()]
            self.assertTrue({'ocr','metadata'} <= {e.get('queue') for e in events if e})
            self.assertTrue(any(e and e['code']=='recovered' for e in events))

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':child(Path(sys.argv[2]),sys.argv[3])
    else:unittest.main()
