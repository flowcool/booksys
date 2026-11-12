"""On-demand technical search. Normal Activity refresh never starts a log scan."""
DIAGNOSTICS_PANE = r'''
<template id="diagnostic-template"><details><summary>Diagnostics techniques</summary>
<p>Choisissez une période UTC de 24 heures au maximum. Les résultats indiquent les lacunes.</p>
<form id="diagnostic-form"><label>Début UTC <input name="from" required></label>
<label>Fin UTC <input name="to" required></label>
<label>Service <select name="service"><option value="">Tous</option><option>collector</option><option>byparr</option><option>library</option><option>retirement</option></select></label>
<label>Étape <select name="stage"><option value="">Toutes</option><option>collection</option><option>byparr</option><option>downloads</option><option>local</option><option>identity</option><option>publication</option><option>retirement</option><option>diagnostics</option></select></label>
<label>Motif <input name="reason" maxlength="64"></label><label>Type de référence <select name="ref_kind"><option value="">Aucun</option><option>source</option><option>package</option><option>file</option><option>origin</option><option>asset</option><option>work</option><option>edition</option><option>decision</option><option>operation</option><option>task</option></select></label>
<label>Référence <input name="ref" maxlength="128"></label><label>Cycle <input name="cycle_id" maxlength="128"></label>
<button type="submit">Rechercher</button><button type="button" id="diagnostic-more" disabled>Suite</button></form>
<p id="diagnostic-status" role="status"></p><pre id="diagnostic-results" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre></details></template>
<script>(()=>{const template=document.getElementById('diagnostic-template');document.getElementById('activity-pane').append(template.content.cloneNode(true));template.remove();
const form=document.getElementById('diagnostic-form'),more=document.getElementById('diagnostic-more'),status=document.getElementById('diagnostic-status'),results=document.getElementById('diagnostic-results');
let cursor=null,query=null,loading=false;const end=new Date();form.elements['to'].value=end.toISOString();form.elements['from'].value=new Date(end-3600000).toISOString();
async function search(continued){if(loading)return;loading=true;more.disabled=true;try{if(!continued){query=new URLSearchParams();for(const [key,value]of new FormData(form))if(value)query.set(key,value);cursor=null;results.textContent=''}if(cursor)query.set('cursor',cursor);else query.delete('cursor');
const response=await fetch('/api/diagnostics?'+query,{cache:'no-store'});const page=await response.json();if(!response.ok){status.textContent=response.status===429?'Lecteur occupé. Réessayez dans une seconde.':'Recherche indisponible.';return}
status.textContent=page.state+(page.gaps?.length?' · Lacunes : '+JSON.stringify(page.gaps):'')+(Object.keys(page.unsupported_records||{}).length?' · Lignes non structurées : '+JSON.stringify(page.unsupported_records):'');for(const event of page.events||[]){results.textContent+=JSON.stringify(event)+'\n';if(results.textContent.length>524288)results.textContent=results.textContent.slice(-524288)}cursor=page.cursor;more.disabled=!cursor
}catch(error){status.textContent='Recherche indisponible.'}finally{loading=false}}
form.onsubmit=event=>{event.preventDefault();search(false)};more.onclick=()=>search(true)})();</script>
'''
