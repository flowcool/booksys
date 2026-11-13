import json
import re
import shutil
import subprocess
import unittest

from library.web import PAGE


class WebJavaScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the actual vanilla-JS event test')
    def test_real_script_search_filters_sort_and_refresh_events(self):
        script = re.search(r'<script>(.*?)</script>', PAGE, re.S | re.I).group(1)
        rows = [dict(id='a',title='Zebra',author='Writer',isbn='9780000000001',series='Series',reason='Unknown edition',status='pending',transfer='finished',intake='validated',identity='pending',publication='pending',ocr='ok',fiche='/fiche/a'),
                dict(id='b',title='Album',author='Other',isbn='123',series='Other series',reason='Published',status='published',transfer='finished',intake='validated',identity='identified',publication='published',ocr='ok',fiche='/fiche/b')]
        driver = r'''
const vm=require('vm'),assert=require('assert');let input='';process.stdin.on('data',d=>input+=d);process.stdin.on('end',async()=>{
const payload=JSON.parse(input);class E{constructor(){this.value='';this.children=[];this.textContent='';this.dataset={}}append(...x){this.children.push(...x)}replaceChildren(...x){this.children=[...x]}}
const ids={},heads=['title','status'].map(k=>{const e=new E;e.dataset.key=k;return e}),requests=[];
const doc={getElementById:id=>ids[id]??(ids[id]=new E),createElement:()=>new E,querySelectorAll:q=>q.startsWith('th')?heads:[]};
const response={revision:1,pages:3,counts:{sources:103,origins:104},groups:payload.rows.map(r=>({id:r.id,representative:r,rows:[r]})),facets:Object.fromEntries(['status','transfer','intake','identity','publication'].map(k=>[k,[payload.rows[0][k]]])),cycle:{diagnostics:{dropped_count:2,activity_dropped_count:3}}};
let timer;const context={document:doc,URLSearchParams,fetch:async url=>{requests.push(url);return {ok:true,json:async()=>response}},setInterval:()=>{},setTimeout:f=>{timer=f;return 1},clearTimeout:()=>{},console};vm.runInNewContext(payload.script,context);
const settle=()=>new Promise(r=>setImmediate(r));await settle();assert.equal(ids.rows.children.length,2);assert(ids.cycle.textContent.includes('Diagnostics dropped: 2'));
for(const value of ['writer','9780000000001','series','unknown']){ids.query.value=value;ids.query.oninput();await timer();assert.equal(new URLSearchParams(requests.at(-1).split('?')[1]).get('search'),value)}
ids.identity.value='identified';ids.identity.onchange();await timer();assert(requests.at(-1).includes('identity=identified'));
heads[0].onclick();await timer();assert(requests.at(-1).includes('descending=true'));heads[0].onkeydown({key:'Enter',preventDefault(){}});await timer();assert(requests.at(-1).includes('descending=false'));
const retained=ids.rows.children[0],option=ids.identity.children[0];await ids.refresh.onclick();assert.strictEqual(ids.rows.children[0],retained);assert.strictEqual(ids.identity.children[0],option);
ids.next.onclick();await settle();assert(requests.at(-1).includes('page=2'));ids.previous.onclick();await settle();assert(requests.at(-1).includes('page=1'));
response.pages=1;response.groups=[];ids.publication.value='missing';ids.publication.onchange();await timer();assert.equal(ids.rows.children.length,0);assert.equal(ids.next.disabled,true);
process.stdout.write('Server query, filters, sort, pagination, stable refresh and degradation display passed');
});'''
        result = subprocess.run(['node','-e',driver], input=json.dumps({'script':script,'rows':rows}), capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
