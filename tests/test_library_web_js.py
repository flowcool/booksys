import json
import re
import shutil
import subprocess
import unittest

from library.web import PAGE


class WebJavaScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the actual vanilla-JS event test')
    def test_real_script_search_filters_sort_and_refresh_events(self):
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
await ids.refresh.onclick();assert.equal(fetched,2);process.stdout.write('Search, per-stage filters, header sorting and refresh events passed');
});'''
        result = subprocess.run(['node','-e',driver], input=json.dumps({'script':script,'data':{'rows':rows,'cycle':{},'source':'fixture','generated_at':'now'}}), capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
