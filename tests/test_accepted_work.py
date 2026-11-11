"""One accepted identity through compatibility records and public outputs."""
import copy
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from library.admission import AdmissionPublisher, work_fields
from library.publication import Capacity
from library.readers import sha256
from library.review import decide, report
from library.views import build_projection
from test_library_publish import setup_library
from postgres_fixture import require_postgresql

class AcceptedWorkTests(unittest.TestCase):
    def decision(self,catalog,key,action,values,request):
        case=next(c for c in report(catalog)['cases'].values() if c.get('asset')==key)
        result=decide(catalog,[{'request_id':request,'case':case['id'],'version':case['version'],'action':action,'values':values}],author='flowcool')[0]
        self.assertEqual(result['status'],'applied',result)

    def test_dual_work_only_and_exact_outputs_agree(self):
        for sqlite in (False,True):
            for mode in ('contradictory','work_only','exact'):
                with self.subTest(sqlite=sqlite,mode=mode),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory)
                    catalog,reception,source,key=setup_library(root,self)
                    state=catalog.read()
                    expected='Album' if mode!='work_only' else 'Other work'
                    proposal={'status':'identified','edition_status':'unknown','work':{'title':'Other work','family':'Comics'},'field_conflicts':{},'candidates':[{'title':'Other work','source':'retained reference'}],'reason':'Reference evidence'}
                    if mode!='exact': state['assets'][key]['work_result']=copy.deepcopy(proposal)
                    if mode=='work_only':
                        state['assets'][key].pop('identity')
                        for table in ('works','editions','variants'): state[table].clear()
                    catalog.save(state,expected=state['revision'])
                    if sqlite: require_postgresql(catalog)
                    identity=copy.deepcopy(catalog.get('assets',key).get('identity'))
                    writer=AdmissionPublisher(catalog,reception,root/'library',isolated=True,local_recovery=sqlite,capacity=Capacity(reserve=0))
                    operation=writer.publish_asset(key)
                    state=catalog.read()
                    page=build_projection(root,state=state,captured={},write_covers=False)
                    oid=next(iter(state['origins']))
                    row=page['rows'][0]
                    self.assertEqual(row['title'],expected)
                    self.assertEqual(page['fiches'][oid]['bibliography']['title'],expected)
                    self.assertEqual(work_fields(state,key)['title'],expected)
                    self.assertIn(expected,operation['target'])
                    with zipfile.ZipFile(root/'library'/operation['target']/'book.cbz') as archive:
                        xml=ET.fromstring(archive.read('ComicInfo.xml'))
                    self.assertEqual(xml.findtext('Title'),expected)
                    self.assertEqual(row['edition'],'unknown' if mode=='work_only' else 'confirmed')
                    if mode=='work_only':
                        self.assertIsNone(xml.find('Publisher'))
                        self.assertEqual(state['editions'],{})
                    else:
                        self.assertEqual(state['assets'][key]['identity'],identity)
                        self.assertEqual(xml.findtext('Publisher'),'Publisher')
                        self.assertTrue(state['editions']['edition']['evidence'])
                        self.assertEqual(state['editions']['edition']['selected'],identity['variant'])
                    if mode=='contradictory':
                        self.assertEqual(state['assets'][key]['work_result'],proposal)
                        self.assertIn('conflict',row['reason'].lower())
                    if sqlite:
                        indexed=catalog.backend().operator_page()['groups'][0]['rows'][0]
                        self.assertEqual(indexed['title'],expected)
                    writer.reconcile(asset=key)
                    self.assertEqual(sha256(source),key)

    def test_work_confirmation_then_exact_decision_replays_without_second_identity(self):
        for sqlite in (False,True):
            with self.subTest(sqlite=sqlite),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                catalog,reception,source,key=setup_library(root,self)
                state=catalog.read();state['assets'][key].pop('identity')
                for table in ('works','editions','variants'): state[table].clear()
                catalog.save(state,expected=state['revision'])
                if sqlite: require_postgresql(catalog)
                self.decision(catalog,key,'identify_work',{'work':{'title':'Earlier work'},'evidence':{'basis':'Operator read title'}},'work')
                self.decision(catalog,key,'identify',{'work_id':'confirmed','edition_id':'confirmed','work':{'title':'Confirmed work'},'edition':{'publisher':'Publisher','language':'fr','evidence':{'basis':'Operator read exact edition'}}},'edition')
                before=catalog.read()
                replayed=catalog.replay()
                self.assertEqual(work_fields(replayed,key)['title'],'Confirmed work')
                self.assertEqual(replayed['assets'][key]['identity'],before['assets'][key]['identity'])
                self.assertEqual(len(catalog.ledger()),2)
                self.assertEqual(replayed['assets'][key]['work_decision']['work']['title'],'Earlier work')
                self.assertEqual(work_fields(catalog.replay(),key)['title'],'Confirmed work')
                if sqlite:
                    from library.launcher import verify_startup
                    from library.catalog import Catalog
                    verify_startup(root/'state')
                    restored=Catalog(root/'state').backend()
                    self.assertEqual(work_fields(restored.export(),key)['title'],'Confirmed work')
                    self.assertEqual(restored.get('assets',key)['identity'],before['assets'][key]['identity'])
                    self.assertEqual(len(restored.decisions()),2)
                self.assertEqual(sha256(source),key)

    def test_automatic_exact_evidence_cannot_replace_manual_work(self):
        from library.metadata import enrich
        from test_library_metadata import EMBEDDED, CANDIDATE
        class Provider:
            def get(self, url):
                return {'status': 'unavailable', 'reason': 'Isolated fixture'}
            def discover(self, title, **kwargs):
                return {'status': 'ok', 'candidates': []}
            def resolve_discovery(self, discovery):
                return []
            def source(self, provider, reference):
                return {'status': 'ok', 'candidate': copy.deepcopy(CANDIDATE)}
            def edition(self, code):
                return self.source('', '')
        for title in ('Operator distinct work', CANDIDATE['title']):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                catalog,_,_,key=setup_library(root,self)
                state=catalog.read();state['assets'][key].pop('identity')
                state['assets'][key]['embedded']=copy.deepcopy(EMBEDDED)
                for table in ('works','editions','variants'): state[table].clear()
                catalog.save(state,expected=state['revision'])
                require_postgresql(catalog)
                self.decision(catalog,key,'identify_work',{'work':{'title':title,'summary':'Operator summary'},'evidence':{'basis':'Read title page'}},'manual')
                state=enrich(catalog,key,Provider(),[('openlibrary','fixture')])
                self.assertEqual(work_fields(state,key)['title'],title)
                self.assertEqual(work_fields(state,key)['summary'],'Operator summary')
                if title!=CANDIDATE['title']:
                    self.assertNotIn('identity',state['assets'][key])
                    self.assertEqual(state['assets'][key]['identity_result']['reason_code'],'conflict')
                    self.assertEqual(state['assets'][key]['identity_result']['candidate'],CANDIDATE)
                else:
                    self.assertTrue(state['assets'][key]['identity'])
                self.assertEqual(work_fields(catalog.replay(),key)['title'],title)
