import json
import re
import shutil
import subprocess
import unittest
from library.activity_ui import ACTIVITY_PANE

class ActivityUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for actual page script')
    def test_tab_details_refresh_and_untrusted_titles(self):
        script = re.search(r'<script>(.*?)</script>', ACTIVITY_PANE, re.S).group(1)
        driver = r'''const vm=require('vm'),assert=require('assert');let input='';
process.stdin.on('data',d=>input+=d);process.stdin.on('end',async()=>{
const script=JSON.parse(input);class E{constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.hidden=false;this.open=false}append(...v){this.children.push(...v)}replaceChildren(...v){this.children=[...v]}setAttribute(k,v){this[k]=v}}
const ids={},timers=[];let calls=0;const bad='<img src=x onerror=evil()>',data={observed_at:1000,resources:{},events:{a:{at:1000,stage:'local',operation:bad}},stages:{local:{label:'Local',state:'waiting',counts:{'waiting files':1},freshness:'current',queue_freshness:'current',items:[{title:bad,id:'a',fiche:'javascript:evil()',reason:bad}]}}};
const document={getElementById:id=>ids[id]??(ids[id]=new E('div')),createElement:tag=>new E(tag)};
vm.runInNewContext(script,{document,fetch:async path=>{assert.equal(path,'/api/activity');calls++;return {ok:true,json:async()=>data}},setInterval:(f,ms)=>{assert.equal(ms,30000);timers.push(f)},console});
ids['activity-tab'].onclick();await new Promise(r=>setImmediate(r));assert.equal(calls,1);assert.equal(ids['library-pane'].hidden,true);assert.equal(ids['activity-pane'].hidden,false);
const stage=ids['activity-stages'].children[0];stage.open=true;stage.ontoggle();let all=[];function walk(e){all.push(e);for(const c of e.children)walk(c)}walk(stage);assert(all.some(e=>e.textContent===bad+' · a'));assert(!all.some(e=>e.tag==='img'||e.tag==='a'));
timers[0]();await new Promise(r=>setImmediate(r));assert.equal(calls,2);assert.equal(ids['activity-stages'].children[0].open,true);
ids['library-tab'].onclick();assert.equal(ids['library-pane'].hidden,false);timers[0]();assert.equal(calls,2);
process.stdout.write('Activity tab, safe details and 30-second refresh passed');});'''
        result = subprocess.run(['node', '-e', driver], input=json.dumps(script), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
