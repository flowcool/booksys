"""An inspected retry is explicit, exact and never repeated after uncertainty."""
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from library.primitives import LibraryError
from library.readers import fingerprint
from library.retirement import Retirement
from retirement_history import evidence_digest
from test_retirement import Native


class ResumeRetirementTests(unittest.TestCase):
    def test_inspected_retry_preserves_files_and_fences_drift_and_repeat(self):
        from ops.resume_retirement import resume, native_paths
        self.assertEqual(native_paths({'containers':[
            {'role':'pyload','mounts':[{'Source':'/current/config','Destination':'/config'}]},
            {'role':'retirement','mounts':[{'Source':'/current/reception','Destination':'/opt/booksys/downloads'}]}
        ]}),(Path('/current/reception'),Path('/current/config/scripts')))
        for fault in ('none', 'identity', 'file', 'active', 'hooks', 'old_restart',
                      'digest', 'repeat', 'pointer', 'lost_response'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);reception=root/'reception';reception.mkdir()
                file=reception/'album.cbz';file.write_bytes(b'retained source')
                package={'pid':280,'name':'Booksys-test Album','folder':'.','dest':0,
                         'links':[{'fid':876,'url':'https://host.test/file','name':file.name,'status':0}]}
                client=Native(package)
                pointer={'state':'resolved'}
                class Mutation:
                    owner='owned';journal=root/'collector/package-retirement.yaml'
                    def read(self):return dict(pointer)
                    def resolve(self, proof):pointer.update(state='resolved',proof=proof)
                @contextmanager
                def mutation(owner,journal):yield Mutation()
                client.mutation=mutation
                source='https://www7.bookys-ebooks.com/bandes-dessinees/357961-le-fil-rouge-tome-07'
                record={'phase':'deleting','created_at':'2026-11-10T05:44:00+00:00','source':source,
                        'submission':{},'packages':{'280':copy.deepcopy(package)},'origins':{},'routing':{},
                        'files':{'reception/album.cbz':{'scope':'reception','path':file.name,
                            'fingerprint':list(fingerprint(file)),'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}},
                        'unrelated':{}}
                record['evidence_digest']=evidence_digest(record)
                history={'schema':1,'acquisitions':{source:record}}
                class History:
                    def __init__(self,*args):pass
                    def retire(self,source,value):history['acquisitions'][source]=copy.deepcopy(value)
                restarted=datetime(2026,11,11,tzinfo=timezone.utc).timestamp()
                expected=record['evidence_digest']
                if fault=='identity':client.packages[280]['links'][0]['fid']=999
                if fault=='file':file.write_bytes(b'changed source')
                if fault=='active':client.active=[{'package_id':280}]
                if fault=='hooks':client.extract=True
                if fault=='old_restart':restarted=0
                if fault=='digest':expected='0'*64
                if fault=='repeat':record['qualified_retry']={'state':'intent'}
                if fault=='pointer':pointer.update(state='uncertain',owner='other',journal='other')
                if fault=='lost_response':client.lose_before_removal=True
                service=Retirement(client,SimpleNamespace(directory=root/'state'),reception,
                    root/'collector/monitor.yaml',root/'collector/state.yaml',concurrent=True)
                with patch('ops.resume_retirement.read_history',side_effect=lambda p:copy.deepcopy(history)), \
                     patch('ops.resume_retirement.NativeHistory',History),patch('library.retirement.NativeHistory',History):
                    if fault=='none':
                        self.assertEqual(resume(service,source,expected,restarted,'a'*64)['outcome'],'retired')
                        self.assertEqual(client.removed,[280])
                        self.assertEqual(history['acquisitions'][source]['phase'],'retired')
                        self.assertEqual(file.read_bytes(),b'retained source')
                    else:
                        with self.assertRaises((LibraryError,OSError)):
                            resume(service,source,expected,restarted,'a'*64)
                        self.assertEqual(client.removed,[])
                    if fault in ('none','lost_response'):
                        with self.assertRaises(LibraryError):
                            resume(service,source,expected,restarted,'a'*64)
