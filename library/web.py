"""Compact derived snapshots. No raw catalogue, filesystem browsing or HTTP writes."""
from __future__ import annotations

import html
from html.parser import HTMLParser
import base64
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
from typing import Any

import yaml

from library.catalog import Catalog, digest, now


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists() or path.is_symlink():
        return {}
    value = yaml.load(path.read_text(), Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
    return value if isinstance(value, dict) else {}


def bounded(value: Any, length: int = 240) -> str:
    return str(value or '')[:length]


def source_url(value: Any) -> str:
    text = bounded(value, 1000)
    try:
        parsed = urlsplit(text)
        if (parsed.scheme == 'https' and parsed.hostname in ('catalogue.bnf.fr', 'openlibrary.org',
                'www.bdtheque.com', 'api.mangadex.org', 'mangadex.org')
                and not parsed.username and not parsed.password and parsed.port in (None, 443)):
            return text
    except ValueError:
        pass
    return ''


def fields(value: dict[str, Any], names: tuple[str, ...], limit: int = 500) -> dict[str, str]:
    return {name: bounded(value[name], limit) for name in names if value.get(name) is not None}


def description_text(value: Any) -> str:
    class Text(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self.hidden = 0

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag in ('script', 'style'):
                self.hidden += 1
            elif not self.hidden and tag in ('p', 'div', 'br', 'li'):
                self.parts.append('\n\n')

        def handle_endtag(self, tag: str) -> None:
            if tag in ('script', 'style'):
                self.hidden = max(0, self.hidden - 1)
            elif not self.hidden and tag in ('p', 'div', 'li'):
                self.parts.append('\n\n')

        def handle_data(self, data: str) -> None:
            if not self.hidden:
                self.parts.append(data)
    parser = Text()
    parser.feed(bounded(value, 12000))
    return '\n\n'.join(' '.join(p.split()) for p in ''.join(parser.parts).split('\n\n') if p.strip())[:4000]


def operator_reason(value: Any) -> str:
    reason = bounded(value)
    messages = {
        'Intake budget deferred': 'En attente du prochain cycle : les fichiers terminés sont vérifiés avant les travaux lourds.',
        'Transfer is not confirmed finished': 'Téléchargement non terminé : le fichier sera examiné après sa fin.',
        'Downloader reports an error; inspect pyLoad': 'Le téléchargement signale une erreur. Consulter pyLoad.',
        'Printed ISBN is missing, invalid or contradictory in the bounded window': 'ISBN absent, invalide ou contradictoire dans les pages examinées.',
        'Printed ISBN requires one exact BnF edition': 'Aucune notice BnF unique ne confirme cet ISBN.',
        'Conflicting or missing edition evidence': 'Les preuves de cette édition sont absentes ou contradictoires.',
        'Non-ISBN identity requires two independent upstream sources': 'Sans ISBN, deux sources indépendantes doivent confirmer cette édition.',
        'Printed edition evidence is incomplete': 'Les mentions imprimées ne suffisent pas à confirmer cette édition.',
        'Multipart representation': 'Fichier en plusieurs parties : assemblage non pris en charge.',
        'Multiple image groups: possible multi-work archive': 'Plusieurs groupes d’images : cette archive peut contenir plusieurs ouvrages.',
        'Validated edition published': 'Édition confirmée et publiée.',
        'Unsupported or ambiguous suffix': 'Extension de fichier non prise en charge ou ambiguë.',
        'LibraryError: Nested container or unauthorized archive content': 'Archive imbriquée ou contenu non autorisé.',
        'LibraryError: Active PDF content or attachment': 'PDF avec contenu actif ou pièce jointe : traitement refusé.',
        'Metadata not attempted': 'Métadonnées à examiner lors d’un prochain cycle.',
    }
    return messages.get(reason, reason)


def build_projection(root: Path) -> dict[str, Any]:
    state = Catalog(root / 'state').read()
    cycle = read_yaml(root / 'evidence/cycle.yaml')
    captured = read_yaml(root / 'snapshots/captured.yaml').get('items', {})
    rows: list[dict[str, Any]] = []
    fiches: dict[str, Any] = {}
    covers = root / 'evidence/web-covers'
    if covers.is_symlink():
        raise ValueError('Web cover cache must not be a symlink')
    covers.mkdir(parents=True, exist_ok=True)
    for oid, origin in state['origins'].items():
        asset = state['assets'].get(origin.get('sha256'), {})
        identity = asset.get('identity', {})
        edition = state['editions'].get(identity.get('edition'), {})
        work = state['works'].get(identity.get('work'), {})
        work_result = asset.get('work_result', {})
        if work_result.get('status') == 'identified' and not identity:
            work = work_result['work']
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
        if origin.get('transfer_error'):
            reason = 'Downloader reports an error; inspect pyLoad'
        candidates = [candidate for candidate in [result.get('candidate'), *result.get('candidates', [])]
                      if isinstance(candidate, dict)][:10]
        from library.printed import printed_isbns
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
                if not cover_path.exists():
                    cover_path.write_bytes(data)
                cover_url = '/cover/' + origin['sha256'] + '.png'
        fiches[oid] = {'title': bounded(title), 'bibliography': {**fields(work, ('title', 'author', 'number', 'series')),
            **fields(edition, ('publisher', 'language', 'date', 'revision', 'isbn', 'medium')), 'isbn': isbn or 'unknown'},
            'summary': summary, 'summary_provenance': summary_provenance, 'taxonomy': taxonomy,
            'overrides': fields(overrides, ('title', 'author', 'series', 'genres', 'public', 'summary'), 1000),
            'assertions': assertions, 'candidates': candidate_projection, 'cover': cover_url,
            'work_metadata': work_result, 'admission': admission,
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
<h1>Booksys library lab</h1><p>Read-only snapshots. Original downloads stay intact. No web actions.</p>
<p id="clock" class="muted"></p><p id="cycle"></p><input id="query" placeholder="Search title, author, ISBN, series or reason">
<label>Status <select id="status"></select></label><label>Transfer <select id="transfer"></select></label>
<label>Intake <select id="intake"></select></label><label>Identity <select id="identity"></select></label>
<label>Publication <select id="publication"></select></label>
<button id="refresh">Refresh</button><p id="count"></p><table><thead><tr>
<th data-key="title" tabindex="0">Title / fiche ↕</th><th data-key="status" tabindex="0">Status ↕</th><th data-key="transfer" tabindex="0">Transfer ↕</th><th data-key="intake" tabindex="0">Intake ↕</th><th data-key="ocr" tabindex="0">OCR ↕</th><th data-key="identity" tabindex="0">Identity ↕</th><th data-key="publication" tabindex="0">Publication ↕</th><th data-key="reason" tabindex="0">Reason ↕</th></tr></thead><tbody id="rows"></tbody></table><button id="previous">Previous</button><span id="page"></span><button id="next">Next</button>
<script>let data,sortKey='title',ascending=true,page=1,lastRows='',lastOptions='';const pageSize=50,$=id=>document.getElementById(id),filters=['status','transfer','intake','identity','publication'];
function matching(r,q){return filters.every(k=>!$(k).value||r[k]===$(k).value)&&(!q||['title','author','isbn','series','reason'].map(k=>r[k]||'').join(' ').toLowerCase().includes(q))}
function cells(r){const tr=document.createElement('tr');for(const k of ['title','status','transfer','intake','ocr','identity','publication','reason']){const td=document.createElement('td');td.textContent=r[k];if(k==='title'&&/^\/fiche\/[a-zA-Z0-9_-]{1,64}$/.test(r.fiche||'')){const a=document.createElement('a');a.href=r.fiche;a.textContent=r.title;td.replaceChildren(a)}tr.append(td)}return tr}
function render(){if(!data)return;const q=$('query').value.toLowerCase(),groups=new Map();for(const r of data.rows){const key=r.source_id||r.id;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(r)}
const rows=[...groups.values()].map(all=>({all,matches:all.filter(r=>matching(r,q))})).filter(g=>g.matches.length);
rows.sort((a,b)=>String(a.matches[0][sortKey]||'').localeCompare(String(b.matches[0][sortKey]||''),undefined,{numeric:true})*(ascending?1:-1));
const pages=Math.max(1,Math.ceil(rows.length/pageSize));page=Math.min(page,pages);$('count').textContent=rows.length+' / '+groups.size+' distinct sources | '+data.rows.length+' file origins | confirmed: '+(data.counts?.confirmed_assets??'?')+' | provisional: '+(data.counts?.provisional_assets??'?');$('page').textContent=page+' / '+pages;$('previous').disabled=page===1;$('next').disabled=page===pages;
const visible=rows.slice((page-1)*pageSize,page*pageSize),signature=JSON.stringify(visible);if(signature===lastRows)return;lastRows=signature;$('rows').replaceChildren();for(const g of visible){const tr=cells(g.matches[0]);if(g.all.length>1){const details=document.createElement('details'),summary=document.createElement('summary'),table=document.createElement('table');summary.textContent=g.all.length+' files / mirrors / members';details.append(summary);for(const child of g.all)table.append(cells(child));details.append(table);tr.children[0].append(details)}$('rows').append(tr)}}
async function refresh(){try{const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)throw Error(response.status);data=await response.json();
const options=JSON.stringify(filters.map(k=>[...new Set(data.rows.map(r=>r[k]))].sort()));if(options!==lastOptions){lastOptions=options;for(const key of filters){const select=$(key),prior=select.value;select.replaceChildren();for(const value of ['',...new Set(data.rows.map(r=>r[key]))].sort()){const option=document.createElement('option');option.value=value;option.textContent=value||'All';select.append(option)}select.value=prior}}
$('clock').textContent='Snapshot: '+data.generated_at+' | '+data.source;const c=data.cycle;$('cycle').textContent='Cycle: '+c.status+' | Stage: '+(c.stage||'idle')+' | Started: '+(c.started_at||'unknown')+' | Last finish: '+(c.finished_at||'none')+' | Duration: '+(c.duration_seconds??'?')+' s | Next: '+(c.next_cycle_at||'after completion')+' | Waiting intake: '+(c.intake_backlog??'?')+' | Waiting metadata: '+(c.backlog??'?')+(c.reason?' | '+c.reason:'');render()}
catch(e){$('clock').textContent='Refresh failed; last displayed snapshot may be stale: '+e}}
function reset(){page=1;render()}$('query').oninput=reset;for(const key of filters)$(key).onchange=reset;for(const th of document.querySelectorAll('th[data-key]')){th.onclick=()=>{if(sortKey===th.dataset.key)ascending=!ascending;else{sortKey=th.dataset.key;ascending=true}reset()};th.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();th.onclick()}}}$('previous').onclick=()=>{page--;render()};$('next').onclick=()=>{page++;render()};$('refresh').onclick=refresh;refresh();setInterval(refresh,30000);
</script>'''


def handler(root: Path, *, resolution: Any = None) -> type[BaseHTTPRequestHandler]:
    import secrets
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def allowed_host(self) -> bool:
            host = self.headers.get('Host', '')
            address = str(self.server.server_address[0])  # type: ignore[index]
            port = self.server.server_port  # type: ignore[attr-defined]
            return host == str(address) + ':' + str(port)

        def do_GET(self) -> None:
            if resolution is not None and not self.allowed_host():
                self.respond(403, b'Invalid Host', 'text/plain')
                return
            if resolution is not None and self.path == '/api/resolution/token':
                self.respond(200, json.dumps({'token': token}).encode(), 'application/json')
                return
            if resolution is not None and re.fullmatch(r'/api/resolution/proof/[a-f0-9]{64}', self.path):
                try:
                    piece = resolution.load_piece(self.path.rsplit('/', 1)[1])
                    self.respond(200, base64.b64decode(piece['png'], validate=True), 'image/png')
                except Exception:
                    self.respond(404, b'Proof unavailable', 'text/plain')
                return
            if self.path == '/':
                self.respond(200, PAGE.encode(), 'text/html; charset=utf-8')
                return
            projection = read_yaml(root / 'evidence/dashboard.yaml')
            if self.path == '/api/status':
                if projection:
                    cycle = read_yaml(root / 'evidence/cycle.yaml')
                    projection['cycle'] = {k: cycle[k] for k in ('status', 'stage', 'stage_started_at',
                        'started_at', 'finished_at', 'duration_seconds', 'next_cycle_at', 'reason',
                        'error_source', 'backlog', 'intake_backlog', 'assets', 'origins', 'published') if k in cycle}
                self.respond(200 if projection else 503, json.dumps({k: v for k, v in projection.items()
                    if k != 'fiches'}).encode(), 'application/json; charset=utf-8')
                return
            if re.fullmatch(r'/fiche/[a-zA-Z0-9_-]{1,64}', self.path):
                fiche = projection.get('fiches', {}).get(self.path.removeprefix('/fiche/'))
                if fiche:
                    if resolution is not None:
                        try:
                            state = resolution.state()
                            origin = state['origins'][self.path.removeprefix('/fiche/')]
                            asset = state['assets'].get(origin.get('sha256'), {})
                            decision = asset.get('resolution', {})
                            if decision:
                                fiche = dict(fiche)
                                fiche['pipeline'] = {**fiche['pipeline'],
                                    'operator_decision': decision.get('decision'),
                                    'decision_application': decision.get('status'),
                                    'decision_reason': decision.get('reason', 'Awaiting normal cycle')}
                                identity = asset.get('identity')
                                if identity:
                                    fiche['bibliography'] = {**fields(state['works'][identity['work']], ('title', 'author', 'number', 'series')),
                                        **fields(state['editions'][identity['edition']], ('publisher', 'language', 'date', 'revision', 'isbn'))}
                        except Exception:
                            self.respond(503, b'Decision evidence unavailable; inspect retained state', 'text/plain')
                            return
                    body = fiche_html(fiche)
                    if resolution is not None:
                        from library.resolution_ui import PANEL
                        body += PANEL
                    self.respond(200, body.encode(), 'text/html; charset=utf-8')
                    return
            if re.fullmatch(r'/cover/[0-9a-f]{64}\.png', self.path):
                path = root / 'evidence/web-covers' / self.path.removeprefix('/cover/')
                if (not path.parent.is_symlink() and not path.is_symlink() and path.is_file()
                        and path.stat().st_size <= 1024**2):
                    self.respond(200, path.read_bytes(), 'image/png')
                    return
            self.respond(404, b'Not found', 'text/plain')

        do_HEAD = do_GET

        def rejected_method(self) -> None:
            self.respond(405, b'Read only; GET and HEAD only', 'text/plain')

        def do_POST(self) -> None:
            if resolution is None:
                self.rejected_method()
                return
            from library.catalog import LibraryError
            if (not self.allowed_host() or self.headers.get('Origin') != 'http://' + self.headers.get('Host', '')
                    or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), token)):
                self.respond(403, b'Forbidden origin or token', 'text/plain')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Content-Type') != 'application/json' or not 0 < length <= 65536:
                    raise LibraryError('Expected bounded JSON')
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise LibraryError('Expected object')
                from library.resolution import case_view
                action = self.path.removeprefix('/api/resolution/')
                if action == 'case' and set(value) in ({'origin'}, {'asset'}):
                    state = resolution.state()
                    key = value.get('asset') or state['origins'][value['origin']]['sha256']
                    result = case_view(resolution, key)
                elif action == 'page' and set(value) == {'asset', 'page'}:
                    result = resolution.page(value['asset'], value['page'])
                elif action == 'preview' and set(value) == {'asset', 'values'}:
                    result = resolution.preview(value['asset'], value['values'])
                elif action == 'confirm':
                    result = resolution.confirm(value)
                elif action == 'confirm-work':
                    from library.work_resolution import confirm_work
                    result = confirm_work(resolution, value)
                elif action == 'search' and set(value) == {'query'}:
                    result = resolution.search(value['query'])
                else:
                    raise LibraryError('Unsupported action or fields')
                self.respond(200, json.dumps(result).encode(), 'application/json')
            except (LibraryError, ValueError, KeyError, TypeError, OSError) as exc:
                reason = str(exc)[:500] if isinstance(exc, LibraryError) else 'Action refused. Refresh the case and check evidence.'
                self.respond(409, json.dumps({'reason': reason}).encode(), 'application/json')

        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = rejected_method

        def log_message(self, *_: Any) -> None:
            pass
    return Handler
