"""Compact derived snapshots. No raw catalogue, filesystem browsing or HTTP writes."""
from __future__ import annotations

import html
from html.parser import HTMLParser
import base64
from urllib.parse import urlsplit
import json
from pathlib import Path
import re
from typing import Any

import yaml

from library.catalog import Catalog
from library.primitives import digest, now


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists() or path.is_symlink():
        return {}
    value = yaml.load(path.read_text(), Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
    return value if isinstance(value, dict) else {}


from library.presentation import bounded, source_url, fields, description_text, operator_reason


def build_projection(root: Path, *, state: dict[str, Any] | None = None, captured: dict[str, Any] | None = None, write_covers: bool = True) -> dict[str, Any]:
    state = Catalog(root / 'state').read() if state is None else state
    from library.operational_state import StatusObservations,CollectorState
    from library.postgres import Database
    cycle = StatusObservations(Database.configured(root/'state')).cycle()
    captured = CollectorState(root/'state').captured().get('items',{}) if captured is None else captured
    rows: list[dict[str, Any]] = []
    fiches: dict[str, Any] = {}
    covers = root / 'evidence/web-covers'
    if covers.is_symlink():
        raise ValueError('Web cover cache must not be a symlink')
    if write_covers:
        covers.mkdir(parents=True, exist_ok=True)
    for oid, origin in state['origins'].items():
        asset = state['assets'].get(origin.get('sha256'), {})
        identity = asset.get('identity', {})
        edition = state['editions'].get(identity.get('edition'), {})
        work = state['works'].get(identity.get('work'), {})
        work_result = asset.get('work_result', {})
        from library.work_identity import accepted_work, proposal_conflicts
        conflicts = proposal_conflicts(asset, work)
        work = accepted_work(asset, work, edition) or work
        overrides = asset.get('overrides', {})
        publication = edition.get('publication', {})
        published = bool(publication and publication.get('asset') == origin.get('sha256'))
        hints = list(asset.get('captured_hints', {}).values()) or [captured.get(origin['source'], {})]
        title = overrides.get('title') or work.get('title') or next((hint.get('title') for hint in hints
            if isinstance(hint, dict) and hint.get('title')), Path(origin['path']).name)
        if origin.get('parent_origin') and not identity and not work:
            title = origin.get('member', title)
        automation = asset.get('automation', {})
        result = asset.get('work_result', asset.get('identity_result', {}))
        reason = origin.get('reason') if origin.get('availability') != 'validated' else (
            automation.get('reason') or result.get('reason', 'Metadata not attempted'))
        if conflicts:
            reason = 'Work proposal conflicts with confirmed identity: ' + ', '.join(conflicts)
        if origin.get('transfer_error'):
            reason = 'Downloader reports an error; inspect pyLoad'
        candidates = [candidate for candidate in [result.get('candidate'), *result.get('candidates', [])]
                      if isinstance(candidate, dict)][:10]
        from library.printed_model import printed_isbns
        codes = sorted(printed_isbns(asset.get('printed', {}))[0])
        isbn = ', '.join(codes) or bounded(edition.get('isbn') or asset.get('embedded', {}).get('ISBN'))
        provisional = asset.get('provisional', {}).get('status') == 'published'
        admission = asset.get('admission', {})
        classified = admission.get('status') == 'classified'
        holding = admission.get('status') == 'holding'
        row = {'id': oid, 'source_id': digest(origin['source']), 'title': bounded(title), 'author': bounded(overrides.get('author') or work.get('author')),
               'isbn': isbn, 'series': bounded(overrides.get('series') or work.get('series') or asset.get('embedded', {}).get('Series')),
               'transfer': 'finished' if origin.get('transfer_status') == 0 else 'not finished (code ' + bounded(origin.get('transfer_status', 'unknown')) + ')',
               'intake': bounded(origin['availability']),
               'retirement': bounded(origin.get('retirement', 'live')),
               'ocr': bounded(asset.get('printed', {}).get('status') or
                              (asset.get('printed_attempts') or [{}])[-1].get('status', 'not attempted')),
               'identity': 'contested' if asset.get('contested') else 'identified' if identity or work else 'pending' if asset else 'not attempted',
               'edition': 'confirmed' if identity else 'unknown',
               'publication': 'classified' if classified else 'À identifier' if holding else 'published' if published else 'available_edition_unknown' if provisional else 'pending', 'reason': operator_reason(reason),
               'observed_at': bounded(origin.get('observed_at')), 'attempt_at': bounded(automation.get('updated_at')),
               'fiche': '/fiche/' + oid}
        if origin.get('retirement'):
            row['transfer'] = 'Retiré de pyLoad · dernier état : ' + row['transfer']
        row['kind'] = 'bundle' if origin.get('bundle') else 'bundle_member' if origin.get('parent_origin') else 'file'
        row['parent'] = origin.get('parent_origin', '')
        if origin.get('bundle'):
            row.update(title='Lot : ' + bounded(title), publication='not applicable', identity='not applicable', ocr='not applicable')
        elif origin.get('parent_origin'):
            row['title'] = 'Membre : ' + row['title']
        row['status'] = 'rejected' if row['intake'] == 'rejected' else 'classified' if classified else 'À identifier' if holding else 'published' if published else 'available_edition_unknown' if provisional else 'pending'
        rows.append(row)
        assertions = []
        for assertion in asset.get('assertions', [])[:30]:
            assertions.append({**fields(assertion, ('field', 'raw', 'normalized', 'source', 'upstream',
                                                   'retrieved_at', 'validation')), 'url': source_url(assertion.get('url'))})
        candidate_fields = ('title', 'author', 'number', 'publisher', 'language', 'date', 'revision',
                            'isbn', 'medium', 'source', 'upstream', 'work_ref', 'edition_ref', 'retrieved_at')
        candidate_projection = [{**fields(candidate, candidate_fields), 'url': source_url(candidate.get('url'))}
                                for candidate in candidates]
        summary = overrides.get('summary') or work.get('summary') or work.get('description')
        if not summary:
            summary = next((assertion.get('normalized') for assertion in asset.get('assertions', [])
                            if assertion.get('field') in ('summary', 'description')), None)
        taxonomy = [{**fields(assertion, ('field', 'normalized', 'raw', 'source', 'retrieved_at')),
                     'url': source_url(assertion.get('url'))} for assertion in asset.get('assertions', [])
                    if assertion.get('field') in ('genres', 'public')][:20]
        captured_summary = description_text(captured.get(origin['source'], {}).get('page', {}).get('description'))
        manual_summary = description_text(overrides.get('summary'))
        summary_provenance = 'Operator override' if manual_summary else 'Captured Bookys page' if captured_summary else 'Collected bibliographic metadata' if summary else 'Not collected'
        summary = manual_summary or captured_summary or description_text(summary) or 'Description non disponible'
        cover_url = ''
        if asset.get('cover') and re.fullmatch(r'[0-9a-f]{64}', str(origin.get('sha256', ''))):
            data = base64.b64decode(asset['cover'], validate=True)
            if len(data) <= 1024**2 and data.startswith(b'\x89PNG\r\n\x1a\n'):
                cover_path = covers / (origin['sha256'] + '.png')
                if cover_path.is_symlink():
                    raise ValueError('Web cover cache must not be a symlink')
                if write_covers and not cover_path.exists():
                    cover_path.write_bytes(data)
                cover_url = '/cover/' + origin['sha256'] + '.png'
        fiches[oid] = {'title': bounded(title), 'bibliography': {**fields(work, ('title', 'author', 'number', 'series')),
            **fields(edition, ('publisher', 'language', 'date', 'revision', 'isbn', 'medium')), 'isbn': isbn or 'unknown'},
            'summary': summary, 'summary_provenance': summary_provenance, 'taxonomy': taxonomy,
            'overrides': fields(overrides, ('title', 'author', 'series', 'genres', 'public', 'summary'), 1000),
            'assertions': assertions, 'candidates': candidate_projection, 'cover': cover_url,
            'work_metadata': ({**work_result, 'work': work, 'status': 'review' if asset.get('contested') else 'identified',
                'edition_status': 'confirmed', 'proposal_conflicts': conflicts} if identity else work_result),
            'admission': admission,
            'cover_provenance': 'First page of the local file; not a publisher cover',
            'pipeline': {key: row[key] for key in ('transfer', 'intake', 'retirement', 'ocr', 'identity', 'publication', 'reason', 'observed_at', 'attempt_at')},
            'technical': {**fields(origin, ('pid', 'fid', 'path', 'sha256', 'parent_origin', 'parent_sha256', 'member', 'member_sha256')),
                          **fields(asset, ('format', 'size', 'pages', 'disposition', 'contested')),
                          **fields(automation, ('attempts', 'outcome', 'next_attempt', 'updated_at'))},
            'source_categories': sorted({urlsplit(origin['source']).path.strip('/').split('/')[0]}),
            'category_provenance': 'Captured Bookys URL path only; subcategories not collected',
            'unknowns': 'Missing fields are unknown. Candidate records are not validated identity. No new source was queried for this page.'}
    for oid, origin in state['origins'].items():
        if origin.get('bundle'):
            fiches[oid]['bundle_children'] = [{'member': bounded(child.get('member')), 'fiche': '/fiche/' + cid,
                'validation': bounded(child.get('availability')), 'reason': operator_reason(child.get('reason'))}
                for cid, child in state['origins'].items() if child.get('parent_origin') == oid][:64]
        if origin.get('parent_origin'):
            fiches[oid]['bundle_parent'] = '/fiche/' + origin['parent_origin']
    return {'schema': 1, 'generated_at': now(), 'catalogue_revision': state['revision'],
            'source': 'saved queue and catalogue snapshots; not live pyLoad',
            'cycle': {key: cycle[key] for key in ('status', 'started_at', 'finished_at', 'duration_seconds',
                      'next_cycle_at', 'stage', 'stage_started_at', 'error_source', 'reason', 'backlog', 'intake_backlog', 'assets', 'origins', 'published') if key in cycle},
            'rows': rows, 'counts': {'sources': len({r['source_id'] for r in rows}), 'origins': len(rows),
                'confirmed_assets': len({e['publication']['asset'] for e in state['editions'].values() if e.get('publication')}),
                'classified_assets': sum(a.get('admission', {}).get('status') == 'classified' for a in state['assets'].values()),
                'holding_assets': sum(a.get('admission', {}).get('status') == 'holding' for a in state['assets'].values()),
                'provisional_assets': sum(a.get('provisional', {}).get('status') == 'published' for a in state['assets'].values())}, 'fiches': fiches}


def fiche_html(fiche: dict[str, Any]) -> str:
    def table(values: dict[str, Any]) -> str:
        return '<dl>' + ''.join('<dt>' + html.escape(str(key)) + '</dt><dd>' +
            html.escape(str(value or 'unknown')) + '</dd>' for key, value in values.items()) + '</dl>'
    def records(values: list[dict[str, Any]]) -> str:
        if not values:
            return '<p>Not collected</p>'
        result = ''
        for value in values:
            result += table({key: v for key, v in value.items() if key != 'url'})
            url = source_url(value.get('url'))
            if url:
                result += '<a rel="noreferrer noopener" href="' + html.escape(url, quote=True) + '">Bibliographic source</a>'
        return result
    body: str = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Booksys fiche</title>'
    body += '<style>body{font:16px system-ui;max-width:65em;margin:2em auto;padding:1em}dt{font-weight:bold;margin-top:.5em}dd{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:16em}details{margin:2em 0}</style>'
    body += '<a href="/">Library</a><h1>' + html.escape(fiche['title']) + '</h1>'
    parent = fiche.get('bundle_parent', '')
    if re.fullmatch(r'/fiche/[0-9a-f]{64}', parent):
        body += '<p><a href="' + parent + '">Lot source</a></p>'
    if fiche.get('bundle_children'):
        body += '<h2>Membres du lot</h2>'
        for child in fiche['bundle_children']:
            url = child.get('fiche', '')
            if re.fullmatch(r'/fiche/[0-9a-f]{64}', url):
                body += '<p><a href="' + url + '">' + html.escape(child['member']) + '</a> — ' + html.escape(child['validation']) + ': ' + html.escape(child['reason']) + '</p>'
    if fiche.get('cover'):
        body += '<img src="' + html.escape(fiche['cover'], quote=True) + '" alt="Local first page"><p>' + html.escape(fiche['cover_provenance']) + '</p>'
    body += '<h2>Pipeline and blockers</h2>' + table(fiche['pipeline'])
    body += '<h2>Bibliography</h2>' + table(fiche['bibliography'])
    metadata = fiche.get('work_metadata', {})
    if metadata:
        body += '<h2>Album et classement</h2>' + table({
            'Identification': metadata.get('status'), 'Edition': metadata.get('edition_status', 'unknown'),
            **{k: metadata.get('work', {}).get(k) for k in ('family', 'genres', 'adult', 'recommended_age')},
            'Fields for review': metadata.get('field_conflicts', {}),
            'Proposal conflicts': metadata.get('proposal_conflicts', []),
            'Managed copy': fiche.get('admission', {}).get('target')})
        body += '<details><summary>Sources de l’album</summary><pre>' + html.escape(
            json.dumps(metadata.get('provenance', []), ensure_ascii=False, indent=2)) + '</pre></details>'
    body += '<h2>Description</h2><p>' + html.escape(fiche['summary_provenance']) + '</p><p style="white-space:pre-wrap">' + html.escape(fiche['summary']) + '</p>'
    body += '<h2>Genres and audience assertions</h2>' + records(fiche['taxonomy'])
    body += '<h2>Source categories</h2>' + table({'category': fiche['source_categories'], 'provenance': fiche['category_provenance']})
    body += '<h2>Operator overrides</h2>' + table(fiche['overrides'])
    body += '<h2>Bibliographic candidates (unconfirmed)</h2>' + records(fiche['candidates'])
    body += '<h2>Source assertions and contradictions</h2><p>Disagreements are retained below. Unknown fields are not filled by inference.</p>' + records(fiche['assertions'])
    body += '<p>' + html.escape(fiche['unknowns']) + '</p><details><summary>Files and technical evidence</summary>' + table(fiche['technical']) + '</details>'
    return body


PAGE = r'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Booksys library lab</title><style>body{font:15px system-ui;margin:2em;background:#101923;color:#e4edf5}
input,select,button{padding:.5em;margin:.3em;background:#233546;color:inherit;border:1px solid #657888}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:.5em;border-bottom:1px solid #456;text-align:left}
a{color:#81caff}.muted{color:#a6b6c5}td:last-child{max-width:30em}</style>
<h1>Booksys library lab</h1><nav aria-label="Views"><button id="library-tab" aria-selected="true">Bibliothèque</button><button id="activity-tab" aria-selected="false">Activité</button></nav><section id="library-pane"><p>Read-only snapshots. Original downloads stay intact. No web actions.</p>
<p id="clock" class="muted"></p><p id="cycle"></p><input id="query" placeholder="Search title, author, ISBN, series or reason">
<label>Status <select id="status"></select></label><label>Transfer <select id="transfer"></select></label>
<label>Intake <select id="intake"></select></label><label>Identity <select id="identity"></select></label>
<label>Publication <select id="publication"></select></label>
<button id="refresh">Refresh</button><p id="count"></p><table><thead><tr>
<th data-key="title" tabindex="0">Title / fiche ↕</th><th data-key="status" tabindex="0">Status ↕</th><th data-key="transfer" tabindex="0">Transfer ↕</th><th data-key="intake" tabindex="0">Intake ↕</th><th data-key="ocr" tabindex="0">OCR ↕</th><th data-key="identity" tabindex="0">Identity ↕</th><th data-key="publication" tabindex="0">Publication ↕</th><th data-key="reason" tabindex="0">Reason ↕</th></tr></thead><tbody id="rows"></tbody></table><button id="previous">Previous</button><span id="page"></span><button id="next">Next</button>
<script>let data,sortKey='title',ascending=true,page=1,lastRows='',lastOptions='',loading=false;const $=id=>document.getElementById(id),filters=['status','transfer','intake','identity','publication'];
function cells(r){const tr=document.createElement('tr');for(const k of ['title','status','transfer','intake','ocr','identity','publication','reason']){const td=document.createElement('td');td.textContent=r[k];if(k==='title'&&/^\/fiche\/[a-zA-Z0-9_-]{1,64}$/.test(r.fiche||'')){const a=document.createElement('a');a.href=r.fiche;a.textContent=r.title;td.replaceChildren(a)}tr.append(td)}return tr}

function render(){if(!data)return;const opened=new Set([...document.querySelectorAll('details[data-source][open]')].map(d=>d.dataset.source));
$('count').textContent=data.counts.sources+' distinct sources | '+data.counts.origins+' file origins';$('page').textContent=page+' / '+data.pages;$('previous').disabled=page===1;$('next').disabled=page>=data.pages;
const signature=JSON.stringify(data.groups);if(signature===lastRows)return;lastRows=signature;$('rows').replaceChildren();for(const group of data.groups){const tr=cells(group.representative||group.rows[0]);const details=document.createElement('details'),summary=document.createElement('summary'),table=document.createElement('table');details.dataset.source=group.id;details.open=opened.has(group.id);summary.textContent='Files / mirrors / members';details.append(summary);for(const row of group.rows)table.append(cells(row));details.append(table);
if(group.children_after){const more=document.createElement('button');more.textContent='More files';let after=group.children_after;more.onclick=async()=>{more.disabled=true;try{const response=await fetch('/api/library?source='+encodeURIComponent(group.id)+'&after='+encodeURIComponent(after),{cache:'no-store'});if(!response.ok)throw Error(response.status);const next=await response.json();for(const row of next.rows)table.append(cells(row));after=next.children_after;if(!after)more.remove()}finally{more.disabled=false}};details.append(more)}tr.children[0].append(details);$('rows').append(tr)}}
async function refresh(){if(loading)return;loading=true;try{const query=new URLSearchParams({page:String(page),sort:sortKey,descending:String(!ascending),search:$('query').value});for(const key of filters)if($(key).value)query.set(key,$(key).value);const response=await fetch('/api/library?'+query,{cache:'no-store'});if(!response.ok)throw Error(response.status);data=await response.json();page=Math.min(page,data.pages);
const options=JSON.stringify(data.facets);if(options!==lastOptions){lastOptions=options;for(const key of filters){const select=$(key),prior=select.value;select.replaceChildren();for(const value of ['',...data.facets[key]]){const option=document.createElement('option');option.value=value;option.textContent=value||'All';select.append(option)}select.value=prior}}
$('clock').textContent='Catalogue revision: '+data.revision;const c=data.cycle||{};$('cycle').textContent='Cycle: '+(c.status||'unknown')+' | Stage: '+(c.stage||'idle')+' | Last finish: '+(c.finished_at||'none')+' | Waiting intake: '+(c.intake_backlog??'?')+(c.reason?' | '+c.reason:'')+' | Diagnostics dropped: '+(c.diagnostics?.dropped_count??0)+' | Activity dropped: '+(c.diagnostics?.activity_dropped_count??0)+(c.recovery?' | Recovery: '+(c.recovery.failed?'failed':c.recovery.overdue?'overdue':c.recovery.requested?'requested':'verified'):'');render()}
catch(e){$('clock').textContent='Refresh failed; last displayed snapshot may be stale: '+e}finally{loading=false}}
let pending;function reset(){page=1;clearTimeout(pending);pending=setTimeout(refresh,200)}$('query').oninput=reset;for(const key of filters)$(key).onchange=reset;for(const th of document.querySelectorAll('th[data-key]')){th.onclick=()=>{if(sortKey===th.dataset.key)ascending=!ascending;else{sortKey=th.dataset.key;ascending=true}reset()};th.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();th.onclick()}}}$('previous').onclick=()=>{page--;refresh()};$('next').onclick=()=>{page++;refresh()};$('refresh').onclick=refresh;refresh();setInterval(refresh,30000);
</script></section>'''


from library.activity_ui import ACTIVITY_PANE
PAGE += ACTIVITY_PANE


from library.diagnostic_ui import DIAGNOSTICS_PANE
PAGE += DIAGNOSTICS_PANE
