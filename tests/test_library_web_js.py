import json
import re
import shutil
import subprocess
import unittest

from library.web import PAGE


class WebJavaScriptTests(unittest.TestCase):
    def test_real_script_search_filters_sort_and_refresh_events(self):
        self.assertIsNotNone(shutil.which('node'), 'Required test prerequisite missing: node')
        script = re.search(r'<script>(.*?)</script>', PAGE, re.S).group(1)
        rows = [dict(id='a',title='Zebra',author='Writer',isbn='9780000000001',series='Series',reason='Unknown edition',status='pending',transfer='finished',intake='validated',identity='pending',publication='pending',ocr='ok',fiche='/fiche/a'),
                dict(id='b',title='Album',author='Other',isbn='123',series='Other series',reason='Published',status='published',transfer='finished',intake='validated',identity='identified',publication='published',ocr='ok',fiche='/fiche/b')]
        driver = r'''
const vm=require('vm'),assert=require('assert');let input='';process.stdin.on('data',d=>input+=d);process.stdin.on('end',async()=>{
const payload=JSON.parse(input);class E{constructor(){this.value='';this.children=[];this.textContent='';this.dataset={}}append(...x){this.children.push(...x)}replaceChildren(...x){this.children=[...x]}addEventListener(k,f){this['on'+k]=f}}
const ids={};const heads=['title','status','transfer','intake','ocr','identity','publication','reason'].map(k=>{let e=new E;e.dataset.key=k;return e});
const doc={getElementById:id=>ids[id]??(ids[id]=new E),createElement:()=>new E,querySelectorAll:()=>heads};let fetched=0;
const context={document:doc,fetch:async()=>{fetched++;return {ok:true,json:async()=>payload.data}},setInterval:()=>{},console};vm.runInNewContext(payload.script,context);
await new Promise(r=>setImmediate(r));assert.equal(ids.rows.children.length,2);
for(const q of ['writer','9780000000001','series','unknown']){ids.query.value=q;ids.query.oninput();assert.equal(ids.rows.children.length,q==='series'?2:1)}ids.query.value='';ids.query.oninput();
ids.identity.value='identified';ids.identity.onchange();assert.equal(ids.rows.children.length,1);ids.identity.value='';ids.identity.onchange();
ids.intake.value='validated';ids.intake.onchange();assert.equal(ids.rows.children.length,2);ids.intake.value='';ids.intake.onchange();
heads[0].onclick();assert.equal(ids.rows.children[0].children[0].children[0].textContent,'Zebra');heads[0].onclick();assert.equal(ids.rows.children[0].children[0].children[0].textContent,'Album');
const retained=ids.rows.children[0],option=ids.identity.children[0];await ids.refresh.onclick();assert.equal(fetched,2);assert.strictEqual(ids.rows.children[0],retained);assert.strictEqual(ids.identity.children[0],option);
payload.data.rows=Array.from({length:103},(_,i)=>({...payload.data.rows[0],id:String(i),source_id:String(i),title:'Album '+String(i).padStart(3,'0')}));payload.data.rows.push({...payload.data.rows[0],id:'mirror'});
await ids.refresh.onclick();assert.equal(ids.rows.children.length,50);assert(ids.count.textContent.includes('103 / 103 distinct sources'));assert.equal(ids.rows.children[0].children[0].children.length,2);
ids.next.onclick();assert.equal(ids.rows.children.length,50);ids.next.onclick();assert.equal(ids.rows.children.length,3);assert.equal(ids.next.disabled,true);
ids.query.value='Album 102';ids.query.oninput();assert.equal(ids.rows.children.length,1);assert.equal(ids.page.textContent,'1 / 1');ids.query.value='';ids.query.oninput();assert.equal(ids.rows.children.length,50);
ids.publication.value='missing';ids.publication.onchange();assert.equal(ids.rows.children.length,0);assert.equal(ids.page.textContent,'1 / 1');ids.publication.value='';ids.publication.onchange();assert.equal(ids.rows.children.length,50);
process.stdout.write('Search, per-stage filters, header sorting and refresh events passed');
});'''
        result = subprocess.run(['node','-e',driver], input=json.dumps({'script':script,'data':{'rows':rows,'cycle':{},'source':'fixture','generated_at':'now'}}), capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
