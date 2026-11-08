"""Read-only Activity pane. All source strings enter the DOM through textContent."""
ACTIVITY_PANE = r'''<section id="activity-pane" hidden>
<h2>Activité</h2><p id="activity-clock">Observation en attente.</p><p id="activity-resources"></p>
<p>Collecte → téléchargements → traitement local → identification et copies. Byparr sert la collecte. Le retrait conserve les fichiers locaux.</p>
<div id="activity-stages"></div><details><summary>Événements importants · 24 heures</summary>
<p id="activity-history-start"></p><ol id="activity-events"></ol></details></section>
<script>(()=>{
const el=id=>document.getElementById(id), labels={working:'En cours',waiting:'En attente',sleeping:'Au repos',stopped:'Arrêté',blocked:'Blocage explicite',unknown:'Inconnu'};
const date=v=>v?new Date(v*1000).toLocaleString():'inconnue';
const duration=v=>v===null||v===undefined?'inconnue':Math.floor(v/60)+' min';
function node(tag,value){const e=document.createElement(tag);if(value!==undefined)e.textContent=value;return e}
const pages={};
function details(stage,key){const box=node('div'),list=node('ul'),controls=node('p'),previous=node('button','Précédents'),next=node('button','Suivants'),count=node('span');
const items=stage.items||[];let page=Math.min(pages[key]||1,Math.max(1,Math.ceil(items.length/50)));
function show(){pages[key]=page;list.replaceChildren();previous.disabled=page===1;next.disabled=page*50>=items.length;
count.textContent=' Page '+page+' / '+Math.max(1,Math.ceil(items.length/50))+' · '+items.length+' éléments observés ';
for(const item of items.slice((page-1)*50,page*50)){const li=node('li');
const label=(item.title||'Élément')+' · '+(item.id||'?');if(/^\/fiche\/[0-9a-f]{64}$/.test(item.fiche||'')){const a=node('a',label);a.href=item.fiche;li.append(a)}else li.append(node('span',label));
li.append(node('p',(labels[item.state]||'Inconnu')+' · '+(item.reason||'Motif inconnu')+' · Durée d’attente observée : '+duration(item.waiting_seconds)+' · Prochaine tentative : '+date(item.next_attempt)+' · Observé : '+date(item.observed_at)));list.append(li)}}
previous.onclick=()=>{page--;show()};next.onclick=()=>{page++;show()};controls.append(previous,count,next);box.append(controls,list);
if(stage.details_truncated)box.append(node('p','Liste limitée à 2 000 éléments. Les compteurs couvrent tous les éléments.'));show();return box}

function render(data){el('activity-clock').textContent='Observation partagée : '+date(data.observed_at)+' · Actualisation : 30 secondes';
const r=data.resources||{},space=r.data||{},mem=r.byparr_memory||{},services=r.services||{};
const gib=v=>v===null||v===undefined?'inconnu':(v/1073741824).toFixed(2)+' GiB';
el('activity-resources').textContent='/data libre : '+gib(space.free_bytes)+' ['+space.freshness+' · '+date(space.observed_at)+'] | Byparr : '+gib(mem.used_bytes)+' / '+gib(mem.limit_bytes)+' ['+mem.freshness+' · '+date(mem.observed_at)+'] | Services : '+Object.entries(services.states||{}).map(([k,v])=>k+' '+v).join(', ')+' ['+services.freshness+' · '+date(services.observed_at)+']';
const host=el('activity-stages'),open=new Set(Array.from(host.children).filter(e=>e.open).map(e=>e.dataset.stage));host.replaceChildren();
for(const [key,s] of Object.entries(data.stages||{})){const d=node('details');d.dataset.stage=key;d.open=open.has(key);
d.append(node('summary',s.label+' · '+(labels[s.state]||'Inconnu')+' · '+Object.entries(s.counts||{}).map(([unit,n])=>n+' '+unit).join(' | ')));
d.append(node('p','Source : '+s.freshness+' · Observée : '+date(s.observed_at)+' · Âge : '+duration(s.age_seconds)+' · File : '+s.queue_freshness+' · Observée : '+date(s.queue_observed_at)));
d.append(node('p','Opération : '+(s.operation||'inconnue')+' · Élément : '+(s.title||s.item||'inconnu')+' · Motif : '+(s.reason||'inconnu')));
d.append(node('p',s.no_progress_seconds===null?'Progression jamais observée.':'Aucune progression depuis '+duration(s.no_progress_seconds)+'.'));
d.append(node('p','Attente observée : '+duration(s.waiting_seconds)+' · Prochaine tentative : '+date(s.next_attempt)));
let filled=false;function fill(){if(d.open&&!filled){d.append(details(s,key));filled=true}}d.ontoggle=fill;fill();host.append(d)}
el('activity-history-start').textContent='Historique commencé : '+date(data.history_started_at)+'. Aucun événement antérieur reconstruit.';
const events=el('activity-events');events.replaceChildren();for(const event of Object.values(data.events||{}).sort((a,b)=>b.at-a.at))events.append(node('li',date(event.at)+' · '+event.stage+' · '+(labels[event.state]||event.state)+' · '+event.operation+' · '+(event.title||event.item||'')+' · '+(event.reason||'')))}
async function refresh(){try{const r=await fetch('/api/activity',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);render(await r.json())}catch(e){el('activity-clock').textContent='Lecture indisponible. Les anciennes valeurs restent affichées.'}}
el('activity-tab').onclick=()=>{el('library-pane').hidden=true;el('activity-pane').hidden=false;el('activity-tab').setAttribute('aria-selected','true');el('library-tab').setAttribute('aria-selected','false');refresh()};
el('library-tab').onclick=()=>{el('activity-pane').hidden=true;el('library-pane').hidden=false;el('activity-tab').setAttribute('aria-selected','false');el('library-tab').setAttribute('aria-selected','true')};
setInterval(()=>{if(!el('activity-pane').hidden)refresh()},30000);
})();</script>'''
