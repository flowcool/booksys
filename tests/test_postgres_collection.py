"""Canonical source records and checkpoints do not depend on legacy files."""
from pathlib import Path
import tempfile
import unittest
from library.primitives import empty
from tests.postgres_fixture import create_store

class CollectionStateTests(unittest.TestCase):

    def test_submission_save_preserves_newer_schedule_and_distinct_source_records(self):
        from library.operational_state import CollectorState
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);create_store(self,root,empty());state=CollectorState(root)
            stale=state.monitor();state.patch_monitor(schedule={'not_before':42})
            stale['submissions']['a']={'status':'creating','operation_id':'exact'}
            state.submission('a',stale['submissions']['a'])
            state.submission('b',{'status':'queued','package_id':9})
            again=CollectorState(root).monitor()
            self.assertEqual(again['schedule'],{'not_before':42})
            self.assertEqual(set(again['submissions']),{'a','b'})

    def test_unrelated_source_capture_does_not_overwrite_stale_other_record(self):
        from library.operational_state import CollectorState
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);create_store(self,root,empty());state=CollectorState(root)
            state.capture('a',{'title':'Old A'});state.capture('b',{'title':'Old B'})
            stale=state.captured();state.capture('b',{'title':'New B'})
            stale['items']['a']['title']='New A';state.capture('a',stale['items']['a'])
            self.assertEqual(state.captured()['items'],{'a':{'title':'New A'},'b':{'title':'New B'}})

    def test_capture_keeps_ordered_duplicate_links_and_absent_fields(self):
        from library.operational_state import CollectorState
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=CollectorState(root)
            record={'title':'Exact','downloads':{'target':{'hoster':'h','urls':['a','a','b']},'unresolved':{'hoster':'other'}}}
            state.capture('source',record)
            self.assertEqual(state.captured()['items']['source'],record)
            with store._database.connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM captured_download_links').fetchone()[0],3)
            state.capture('source',{'title':'No invented downloads'})
            self.assertEqual(state.captured()['items']['source'],{'title':'No invented downloads'})





    def test_native_observation_preserves_order_capacity_and_independent_heartbeats(self):
        from library.operational_state import RuntimeObservation
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=RuntimeObservation(store._database)
            packages=[{'pid':2,'dest':1,'links':[{'fid':9,'status':12},{'fid':8,'status':0}]},{'pid':1,'dest':0,'links':[]}]
            state.publish(packages,at=123,remaining=None,overshoot=32)
            self.assertEqual(state.queue(),(packages,123))
            self.assertIsNone(state.capacity()['remaining_bytes'])
            state.heartbeat('intake',at=120);state.heartbeat('metadata',at=121)
            state.heartbeat('intake',at=123)
            self.assertEqual(state.heartbeats(),{'intake':123,'metadata':121})















