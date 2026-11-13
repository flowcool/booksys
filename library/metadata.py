"""Bounded public metadata adapters and conservative deterministic identity."""
from __future__ import annotations

import json
from contextvars import ContextVar
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

from library.catalog import Catalog
from library.primitives import LibraryError, now, lock

from booksys_core.network_admission import RollingBudget

PROVIDER_BUDGET: ContextVar[RollingBudget | None] = ContextVar('current_provider_budget', default=None)

REDIRECT_CHARGE: ContextVar[Callable[[], None] | None] = ContextVar('provider_redirect_charge', default=None)

Fetch = Callable[[str], tuple[int, str]]


from library.bibliographic import normalized, isbn13


def discovery_hint(title: str, embedded: dict[str, Any]) -> dict[str, str]:
    """Normalize captured captions for search only, never for identity proof."""
    title = re.sub(r'\[[^]]*\]|\([^)]*\b(?:pdf|cbr|cbz|epub|scan)\b[^)]*\)', ' ', title, flags=re.I)
    title = re.sub(r'\b(?:PDF|CBR|CBZ|EPUB|HD|FRENCH|MULTI)\b', ' ', title, flags=re.I)
    title = re.sub(r'\s*[-–|]\s*$', '', title)
    volume = re.search(r'\b(?:tome|vol(?:ume)?|t)\.?\s*(\d+)\b', title, re.I)
    return {'title': ' '.join(title.split())[:200], 'author': str(embedded.get('Writer', ''))[:120],
            'number': str(embedded.get('Number', '')) or (volume.group(1) if volume else '')}


class QualifiedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        target = urlsplit(urljoin(req.full_url, newurl))
        if (target.scheme != 'https' or target.hostname != urlsplit(req.full_url).hostname
                or target.username or target.password or target.port):
            raise LibraryError('Provider redirect escapes qualified origin')
        charge = REDIRECT_CHARGE.get()
        if charge is not None:
            charge()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_fetch(url: str) -> tuple[int, str]:
    try:
        request = Request(url, headers={'User-Agent': 'Booksys/0.1 (personal metadata catalogue)',
                                       'Accept': 'application/json,text/html'})
        # Network reads have finite socket timeouts so an unavailable peer cannot hold a worker forever.
        with build_opener(QualifiedRedirect).open(request, timeout=15) as response:
            # Read at most 2 MiB of provider HTML or JSON.
            body = response.read(2 * 1024 ** 2 + 1)
            if len(body) > 2 * 1024 ** 2:
                raise LibraryError('Provider response exceeds limit')
            return response.status, body.decode('utf-8')
    except HTTPError as exc:
        admission = PROVIDER_BUDGET.get()
        if admission is not None and (exc.code == 429 or exc.headers.get('Retry-After') is not None):
            admission.remote_deadline(exc.headers.get('Retry-After'))
        return exc.code, ''


class Providers:
    def __init__(self, cache: Path, *, fetch: Fetch = http_fetch, interval: float = 1,
                 budget: int = 12, shared: Path | None = None):
        self.cache, self.fetch, self.interval, self.budget = cache, fetch, interval, budget
        self.shared = shared
        from booksys_core.network_admission import configured_budget
        self.admission = configured_budget('provider')
        if self.admission is None and shared is not None:
            self.admission = RollingBudget(shared/'provider-admission.yaml')
        from library.operational_state import ProviderCache
        self._cache=ProviderCache(shared or (self.admission.path.parent if self.admission else cache.parent),str(cache.absolute()))
        self.last_request = 0.0
        self.events: list[dict[str, Any]] = []

    def get(self, url: str) -> dict[str, Any]:
        if self.shared is None:
            result = self._get(url)
        else:
            with lock(self.shared / 'library-network.lock'):
                result = self._get(url)
        self.events.append({key: result[key] for key in ('status', 'http_status', 'reason', 'dependency', 'retrieved_at', 'expires') if key in result})
        return result

    def _get(self, url: str) -> dict[str, Any]:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname not in
                ('openlibrary.org', 'catalogue.bnf.fr', 'www.bedetheque.com')
                or parts.username or parts.password or parts.port):
            raise LibraryError('Unqualified provider URL')
        cached=self._cache.get(url)
        if cached is not None and cached['expires']>time.time():
            return cached
        if self.admission is None and self.budget <= 0:
            return {'status': 'unavailable', 'reason': 'Request budget exhausted', 'url': url}
        if self.admission is None:
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        try:
            self.charge()
        except LibraryError as exc:
            return {'status': 'unavailable', 'reason': str(exc), 'url': url}
        self.last_request = time.monotonic()
        try:
            context = REDIRECT_CHARGE.set(self.charge)
            budget_context = PROVIDER_BUDGET.set(self.admission)
            try:
                status, body = self.fetch(url)
            finally:
                REDIRECT_CHARGE.reset(context)
                PROVIDER_BUDGET.reset(budget_context)
            result = {'status': 'ok' if status == 200 else 'unavailable',
                      'http_status': status, 'body': body if status == 200 else '',
                      'url': url, 'retrieved_at': now(),
                      'expires': time.time() + (7 * 86400 if status == 200 else 300)}
        except (OSError, ValueError, LibraryError) as exc:
            if str(exc) == 'Request budget exhausted':
                return {'status': 'unavailable', 'reason': str(exc), 'url': url}
            result = {'status': 'unavailable', 'reason': str(exc), 'url': url,
                      'retrieved_at': now(), 'expires': time.time() + 300}
        result['dependency'] = parts.hostname
        if self.admission is not None:
            from library.dependency_recovery import DependencyRecovery
            response_status = int(result.get('http_status', 0))
            DependencyRecovery(self.admission.path.parent/'dependency-readiness.yaml').observe(
                str(parts.hostname), available=(response_status not in (0, 429)
                    and response_status < 500), at=time.time(), url=url)
        self._cache.retain(url,result)
        return result

    def charge(self) -> None:
        if self.admission is None and self.budget <= 0:
            raise LibraryError('Request budget exhausted')
        if self.admission is not None:
            from booksys_core.network_admission import BudgetWait
            from booksys_core.errors import CollectionError
            try:
                admission = self.admission
                admission.debit()
            except BudgetWait as exc:
                # Redirects debit the same budget. Wait only for a short pacing
                # gap. A window limit or remote deadline still defers the task.
                delay = exc.due - time.time()
                if not 0 < delay <= self.interval:
                    raise LibraryError('Request budget exhausted') from exc
                time.sleep(delay)
                try:
                    admission.debit()
                except BudgetWait as retry:
                    raise LibraryError('Request budget exhausted') from retry
                except CollectionError as retry:
                    raise LibraryError('Network authority unavailable') from retry
            except CollectionError as exc:
                raise LibraryError('Network authority unavailable') from exc
            return
        self.budget -= 1

    def discover(self, title: str, *, author: str = '', number: str = '') -> dict[str, Any]:
        query: dict[str, Any] = {'title': title[:200], 'limit': 5,
            'fields': 'key,title,author_name,isbn,edition_key,publisher,language,first_publish_year'}
        if author:
            query['author'] = author[:120]
        # Volume stays in the title query; OpenLibrary has no qualified volume index.
        if number and not re.search(r'\b(?:vol(?:ume)?|tome|t)\.?\s*' + re.escape(number) + r'\b', title, re.I):
            query['title'] += ' volume ' + number
        url = 'https://openlibrary.org/search.json?' + urlencode(query)
        response = self.get(url)
        if response['status'] != 'ok':
            return response
        try:
            data = json.loads(response['body'])
            if not isinstance(data, dict) or not isinstance(data.get('docs'), list):
                raise LibraryError('Unexpected OpenLibrary search response')
            return {'status': 'ok', 'candidates': data['docs'][:5], 'url': url,
                    'retrieved_at': response['retrieved_at']}
        except (ValueError, LibraryError, TypeError) as exc:
            return {'status': 'unavailable', 'reason': str(exc), 'url': url}

    def resolve_discovery(self, discovery: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve at most three search hits. Search titles never establish identity."""
        attempts = []
        seen = set()
        for hit in discovery.get('candidates', [])[:5]:
            if not isinstance(hit, dict):
                continue
            references = hit.get('edition_key', [])
            if not isinstance(references, list):
                continue
            for reference in references:
                if isinstance(reference, str) and re.fullmatch(r'OL\d+M', reference) and reference not in seen:
                    seen.add(reference)
                    attempts.append(self.edition(reference))
                    break
            if len(attempts) == 3:
                break
        return attempts

    def edition(self, reference: str) -> dict[str, Any]:
        code = isbn13(reference)
        if code:
            url = f'https://openlibrary.org/isbn/{code}.json'
        elif re.fullmatch(r'OL\d+M', reference):
            url = f'https://openlibrary.org/books/{reference}.json'
        else:
            raise LibraryError('Invalid OpenLibrary edition reference')
        response = self.get(url)
        if response['status'] != 'ok':
            return response
        try:
            data = json.loads(response['body'])
            if not isinstance(data, dict) or not isinstance(data.get('type'), dict):
                raise LibraryError('Malformed OpenLibrary edition record')
            for field in ('authors', 'languages', 'works'):
                if not isinstance(data.get(field, []), list) or any(not isinstance(item, dict)
                        or not isinstance(item.get('key'), str) for item in data.get(field, [])):
                    raise LibraryError('Malformed OpenLibrary edition links')
            for field in ('publishers', 'isbn_13', 'source_records'):
                if not isinstance(data.get(field, []), list) or any(not isinstance(item, str)
                        for item in data.get(field, [])):
                    raise LibraryError('Malformed OpenLibrary edition values')
            if any(not isinstance(data.get(field, ''), str) for field in ('title', 'publish_date', 'edition_name', 'key')):
                raise LibraryError('Malformed OpenLibrary edition fields')
            if (data.get('type', {}).get('key') != '/type/edition' or
                    (code and code not in data.get('isbn_13', [])) or
                    (not code and data.get('key') != '/books/' + reference)):
                raise LibraryError('Unexpected OpenLibrary edition response')
            authors = data.get('authors', [])
            author = ''
            if len(authors) == 1 and re.fullmatch(r'/authors/OL\d+A', authors[0].get('key', '')):
                result = self.get('https://openlibrary.org' + authors[0]['key'] + '.json')
                if result['status'] == 'ok':
                    author_data = json.loads(result['body'])
                    if not isinstance(author_data, dict) or not isinstance(author_data.get('name', ''), str):
                        raise LibraryError('Malformed OpenLibrary author')
                    author = author_data.get('name', '')
            languages = data.get('languages', [])
            language = languages[0]['key'].split('/')[-1] if len(languages) == 1 else ''
            language = {'eng': 'en', 'fre': 'fr', 'fra': 'fr', 'jpn': 'ja'}.get(language, language)
            publishers = data.get('publishers', [])
            works = data.get('works', [])
            volume = re.search(r'\b(?:vol(?:ume)?\.?|tome)\s*(\d+)\b', data['title'], re.I)
            candidate = {'source': 'openlibrary', 'upstream': 'openlibrary', 'url': url,
                'retrieved_at': response['retrieved_at'], 'isbn': code or next((value for value in
                    data.get('isbn_13', []) if isinstance(value, str) and isbn13(value)), None), 'title': data['title'],
                'author': author, 'number': volume.group(1) if volume else '',
                'language': language, 'publisher': publishers[0] if len(publishers) == 1 else '',
                'date': data.get('publish_date', ''), 'revision': data.get('edition_name', ''),
                'work_ref': works[0]['key'] if len(works) == 1 else '', 'edition_ref': data['key'],
                'medium': 'print' if normalized(data.get('physical_format', '')) in
                    ('paperback', 'hardcover', 'hardback', 'softcover', 'broché', 'relié') else 'unknown',
                'unnumbered': not volume and len(works) == 1,
                'source_records': data.get('source_records', [])}
            if any('bnf' in record.casefold() for record in candidate['source_records']):
                candidate['upstream'] = 'bnf'
            return {'status': 'ok', 'candidate': candidate}
        except (ValueError, KeyError, TypeError, LibraryError) as exc:
            return {'status': 'unavailable', 'reason': str(exc), 'url': url}

    def source(self, provider: str, reference: str) -> dict[str, Any]:
        if provider == 'bnf' and isbn13(reference):
            from library.bnf import parse_bnf
            url = 'https://catalogue.bnf.fr/api/SRU?' + urlencode({
                'version': '1.2', 'operation': 'searchRetrieve',
                'query': f'bib.fuzzyISBN all \"{reference}\"',
                'recordSchema': 'unimarcxchange', 'maximumRecords': 2})
            response = self.get(url)
            if response['status'] != 'ok':
                return response
            try:
                return {'status': 'ok', 'candidate': parse_bnf(response['body'], reference,
                    url, response['retrieved_at'])}
            except LibraryError as exc:
                return {'status': 'not_found' if str(exc) == 'BnF edition not found' else 'unavailable',
                        'reason': str(exc), 'url': url}
        if provider == 'openlibrary':
            return self.edition(reference)
        raise LibraryError('Invalid provider or reference')


def add_assertions(asset: dict[str, Any], response: dict[str, Any]) -> None:
    asset.setdefault('source_attempts', []).append({k: v for k, v in response.items() if k not in ('body', 'fields')})
    asset['source_attempts'] = asset['source_attempts'][-50:]


def prepare_enrichment(store: Catalog, asset_id: str, providers: Providers,
           references: list[tuple[str, str]]) -> dict[str, Any]:
    state = store.result_set('identify', {'assets': [asset_id]})
    asset = state['assets'][asset_id]
    from library.work_enrichment import enrich_work
    enrich_work(state, asset_id, providers)
    if asset['work_result']['status'] == 'identified' and not references:
        return state
    # All external work happens on the snapshot, outside catalogue locks.
    candidates = []
    for provider, ref in references:
        response = providers.source(provider, ref)
        add_assertions(asset, response)
        if response['status'] == 'ok' and 'candidate' in response:
            candidates.append(response['candidate'])
    embedded = asset.get('embedded', {})
    code = isbn13(str(embedded.get('ISBN', embedded.get('GTIN', ''))))
    title = str(embedded.get('Title', ''))
    hint_source = 'embedded:Title'
    if not title:
        for source, hint in sorted(asset.get('captured_hints', {}).items()):
            if isinstance(hint, dict) and isinstance(hint.get('title'), str) and hint['title'].strip():
                title, hint_source = hint['title'], source
                break
    printed = asset.get('printed')
    printed_code = None
    if printed:
        from library.printed_model import printed_isbns
        codes, invalid = printed_isbns(printed)
        if printed.get('status') == 'ok' and len(codes) == 1 and not invalid:
            printed_code = next(iter(codes))
    if title and not printed_code:
        hint = discovery_hint(title, embedded)
        asset['discovery_hint'] = {**hint, 'raw_title': title, 'source': hint_source, 'validation': 'discovery_only'}
        asset['discovery'] = providers.discover(hint['title'], author=hint['author'], number=hint['number'])
        attempts = providers.resolve_discovery(asset['discovery'])
        asset['discovery']['edition_attempts'] = attempts
        candidates.extend(attempt['candidate'] for attempt in attempts
                          if attempt['status'] == 'ok' and 'candidate' in attempt)
    if code:
        response = providers.edition(code)
        asset.setdefault('source_attempts', []).append({k: v for k, v in response.items() if k != 'body'})
        if response['status'] == 'ok':
            candidates.append(response['candidate'])
    if not code and not printed_code:
        for candidate_code in list(dict.fromkeys(c.get('isbn') for c in candidates if isbn13(str(c.get('isbn', '')))))[:3]:
            response = providers.source('bnf', candidate_code)
            add_assertions(asset, response)
            if response['status'] == 'ok':
                candidates.append(response['candidate'])
    candidates = list({(candidate['source'], candidate['edition_ref']): candidate
                       for candidate in candidates}.values())
    printed = asset.get('printed')
    if printed:
        from library.printed_model import printed_isbns
        if printed.get('asset') != asset_id:
            raise LibraryError('Printed assertions are not bound to this asset')
        codes, invalid = printed_isbns(printed)
        if printed.get('status') == 'ok' and len(codes) == 1 and not invalid:
            for provider in ('bnf', 'openlibrary'):
                response = providers.source(provider, next(iter(codes)))
                add_assertions(asset, response)
                if response['status'] == 'ok':
                    candidates.append(response['candidate'])
        candidates = list({(candidate['source'], candidate['edition_ref']): candidate
                           for candidate in candidates}.values())
    enrich_work(state, asset_id, providers, candidates)
    return state


def enrich(store: Catalog, asset_id: str, providers: Providers,
           references: list[tuple[str, str]]) -> dict[str, Any]:
    state = prepare_enrichment(store, asset_id, providers, references)
    store.save(state, expected=state['revision'])
    return store.working_set('identify', {'assets': [asset_id]})
