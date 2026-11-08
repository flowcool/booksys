"""Bounded public metadata adapters and conservative deterministic identity."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml
from bs4 import BeautifulSoup

from library.catalog import Catalog, LibraryError, apply_effect, atomic_yaml, digest, now

Fetch = Callable[[str], tuple[int, str]]


def normalized(value: Any) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value)).casefold().split())


def isbn13(value: str) -> str | None:
    value = re.sub(r'[-\s]', '', value)
    if not re.fullmatch(r'97[89]\d{10}', value):
        return None
    checksum = sum(int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(value[:12]))
    return value if (10 - checksum % 10) % 10 == int(value[-1]) else None


def identify(embedded: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    upstreams = {c.get('upstream', c['source']) for c in candidates}
    outcome: dict[str, Any] = {'status': 'pending', 'independent_sources': len(upstreams),
                              'reason': 'No consistent exact edition identifier', 'candidates': candidates}
    code = isbn13(str(embedded.get('ISBN', embedded.get('GTIN', ''))))
    matches = [c for c in candidates if c.get('isbn') == code] if code else candidates
    # Multiple providers describing one edition must agree on all discriminators.
    identities = {(normalized(c.get('title', '')), normalized(c.get('author', '')),
                   normalized(c.get('number', '')), normalized(c.get('language', '')),
                   normalized(c.get('publisher', '')), normalized(c.get('date', '')),
                   normalized(c.get('revision', ''))) for c in matches}
    if not matches or len(identities) != 1:
        return {**outcome, 'reason': 'Conflicting or missing edition evidence'}
    if not code and len({c.get('upstream', c['source']) for c in matches}) < 2:
        return {**outcome, 'reason': 'Non-ISBN identity requires two independent upstream sources'}
    candidate = matches[0]
    required = ('title', 'author', 'number', 'publisher', 'language', 'date',
                'revision', 'work_ref', 'edition_ref', 'url', 'source', 'upstream')
    if any(not candidate.get(field) for field in required):
        return {**outcome, 'reason': 'Edition evidence is incomplete'}
    for source_field, field in [('Title', 'title'), ('Writer', 'author'), ('Number', 'number'),
                                ('Publisher', 'publisher'), ('LanguageISO', 'language')]:
        if not embedded.get(source_field) or normalized(embedded[source_field]) != normalized(candidate[field]):
            return {**outcome, 'reason': f'Missing or conflicting {field}'}
    if not code:
        for source_field, field in [('EditionDate', 'date'), ('EditionRevision', 'revision')]:
            if not embedded.get(source_field) or normalized(embedded[source_field]) != normalized(candidate[field]):
                return {**outcome, 'reason': f'Missing or conflicting non-ISBN {field}'}
    # ISBN says which edition. Matching independent content fields guard against
    # copied/wrong ComicInfo assertions. Bookys titles and filenames never suffice.
    return {**outcome, 'status': 'identified', 'reason': ('Exact ISBN and five consistent content fields'
            if code else 'Two independent upstream sources and seven consistent bibliographic fields'),
            'candidate': candidate, 'evidence': {'isbn': code, 'url': candidate['url'],
            'fields': ['title', 'author', 'number', 'publisher', 'language', 'date', 'revision'],
            'sources': [{'url': c['url'], 'upstream': c['upstream']} for c in matches], 'retrieved_at': now()}}


def parse_bdtheque(body: str) -> dict[str, Any]:
    soup = BeautifulSoup(body, 'html.parser')
    title = soup.find('h1')
    if not title or any(word in normalized(title.get_text()) for word in ('maintenance', 'forbidden', 'captcha')):
        raise LibraryError('BDTheque response is not a series page')
    result: dict[str, Any] = {'title': title.get_text(' ', strip=True), 'genres': [], 'public': None}
    for row in soup.select('table.table-sm tr'):
        cells = row.find_all('td')
        if len(cells) < 2:
            continue
        label = normalized(cells[0].get_text(' ', strip=True))
        if 'genre' in label:
            for link in cells[1].select('a[href]'):
                href, value = str(link['href']), link.get_text(' ', strip=True)
                if 'public=' in href:
                    result['public'] = value
                elif 'genre=' in href:
                    result['genres'].append(value)
        elif 'scénario' in label or 'scenario' in label:
            result['author'] = cells[1].get_text(' ', strip=True)
    return result


def parse_mangadex(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data.get('attributes'), dict) or not data['attributes'].get('title'):
        raise LibraryError('MangaDex response is not a manga')
    attrs = data['attributes']
    genres = []
    for tag in attrs.get('tags', []):
        item = tag.get('attributes', {})
        if item.get('group') == 'genre':
            names = item.get('name', {})
            value = names.get('fr') or names.get('en')
            if value:
                genres.append(value)
    return {'title': next(iter(attrs['title'].values())), 'genres': genres,
            'public': attrs.get('contentRating')}


class QualifiedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        target = urlsplit(urljoin(req.full_url, newurl))
        if (target.scheme != 'https' or target.hostname != urlsplit(req.full_url).hostname
                or target.username or target.password or target.port):
            raise LibraryError('Provider redirect escapes qualified origin')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_fetch(url: str) -> tuple[int, str]:
    try:
        request = Request(url, headers={'User-Agent': 'Booksys/0.1 (personal metadata catalogue)',
                                       'Accept': 'application/json,text/html'})
        with build_opener(QualifiedRedirect).open(request, timeout=15) as response:
            body = response.read(2 * 1024 ** 2 + 1)
            if len(body) > 2 * 1024 ** 2:
                raise LibraryError('Provider response exceeds limit')
            return response.status, body.decode('utf-8')
    except HTTPError as exc:
        return exc.code, ''


class Providers:
    def __init__(self, cache: Path, *, fetch: Fetch = http_fetch, interval: float = 1,
                 budget: int = 12):
        self.cache, self.fetch, self.interval, self.budget = cache, fetch, interval, budget
        self.last_request = 0.0

    def get(self, url: str) -> dict[str, Any]:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname not in
                ('openlibrary.org', 'www.bdtheque.com', 'api.mangadex.org')
                or parts.username or parts.password or parts.port):
            raise LibraryError('Unqualified provider URL')
        path = self.cache / (digest(url) + '.yaml')
        if path.exists():
            cached: dict[str, Any] = yaml.safe_load(path.read_text())
            if cached['expires'] > time.time():
                return cached
        if self.budget <= 0:
            return {'status': 'unavailable', 'reason': 'Request budget exhausted', 'url': url}
        time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        self.budget -= 1
        self.last_request = time.monotonic()
        try:
            status, body = self.fetch(url)
            result = {'status': 'ok' if status == 200 else 'unavailable',
                      'http_status': status, 'body': body if status == 200 else '',
                      'url': url, 'retrieved_at': now(),
                      'expires': time.time() + (7 * 86400 if status == 200 else 300)}
        except (OSError, ValueError, LibraryError) as exc:
            result = {'status': 'unavailable', 'reason': str(exc), 'url': url,
                      'retrieved_at': now(), 'expires': time.time() + 300}
        atomic_yaml(path, result)
        return result

    def discover(self, title: str) -> dict[str, Any]:
        url = 'https://openlibrary.org/search.json?' + urlencode({'title': title[:200], 'limit': 5,
            'fields': 'key,title,author_name,isbn,edition_key,publisher,language,first_publish_year'})
        response = self.get(url)
        if response['status'] != 'ok':
            return response
        try:
            data = json.loads(response['body'])
            if not isinstance(data.get('docs'), list):
                raise LibraryError('Unexpected OpenLibrary search response')
            return {'status': 'ok', 'candidates': data['docs'][:5], 'url': url,
                    'retrieved_at': response['retrieved_at']}
        except (ValueError, LibraryError, TypeError) as exc:
            return {'status': 'unavailable', 'reason': str(exc), 'url': url}

    def edition(self, code: str) -> dict[str, Any]:
        if not isbn13(code):
            raise LibraryError('Invalid ISBN-13')
        url = f'https://openlibrary.org/isbn/{code}.json'
        response = self.get(url)
        if response['status'] != 'ok':
            return response
        try:
            data = json.loads(response['body'])
            if data.get('type', {}).get('key') != '/type/edition' or code not in data.get('isbn_13', []):
                raise LibraryError('Unexpected OpenLibrary edition response')
            authors = data.get('authors', [])
            author = ''
            if len(authors) == 1 and re.fullmatch(r'/authors/OL\d+A', authors[0].get('key', '')):
                result = self.get('https://openlibrary.org' + authors[0]['key'] + '.json')
                if result['status'] == 'ok':
                    author = json.loads(result['body']).get('name', '')
            languages = data.get('languages', [])
            language = languages[0]['key'].split('/')[-1] if len(languages) == 1 else ''
            language = {'eng': 'en', 'fre': 'fr', 'fra': 'fr', 'jpn': 'ja'}.get(language, language)
            publishers = data.get('publishers', [])
            works = data.get('works', [])
            volume = re.search(r'\b(?:vol(?:ume)?\.?|tome)\s*(\d+)\b', data['title'], re.I)
            candidate = {'source': 'openlibrary', 'upstream': 'openlibrary', 'url': url,
                'retrieved_at': response['retrieved_at'], 'isbn': code, 'title': data['title'],
                'author': author, 'number': volume.group(1) if volume else '',
                'language': language, 'publisher': publishers[0] if len(publishers) == 1 else '',
                'date': data.get('publish_date', ''), 'revision': data.get('edition_name', ''),
                'work_ref': works[0]['key'] if len(works) == 1 else '', 'edition_ref': data['key'],
                'source_records': data.get('source_records', [])}
            return {'status': 'ok', 'candidate': candidate}
        except (ValueError, KeyError, TypeError, LibraryError) as exc:
            return {'status': 'unavailable', 'reason': str(exc), 'url': url}

    def source(self, provider: str, reference: str) -> dict[str, Any]:
        if provider == 'bdtheque' and re.fullmatch(r'\d+/[a-z0-9-]+', reference):
            url = f'https://www.bdtheque.com/series/{reference}'
            parser = parse_bdtheque
        elif provider == 'mangadex' and re.fullmatch(r'[0-9a-f-]{36}', reference):
            url = f'https://api.mangadex.org/manga/{reference}'
            parser = lambda body: parse_mangadex(json.loads(body)['data'])
        else:
            raise LibraryError('Invalid provider or reference')
        response = self.get(url)
        if response['status'] != 'ok':
            return response
        try:
            result = parser(response['body'])
            return {'status': 'ok', 'source': provider, 'upstream': provider, 'url': url,
                    'retrieved_at': response['retrieved_at'], 'fields': result}
        except (ValueError, KeyError, LibraryError) as exc:
            return {'status': 'unavailable', 'reason': str(exc), 'url': url}


def add_assertions(asset: dict[str, Any], response: dict[str, Any]) -> None:
    asset.setdefault('source_attempts', []).append({k: v for k, v in response.items() if k not in ('body', 'fields')})
    asset['source_attempts'] = asset['source_attempts'][-50:]
    if response['status'] != 'ok' or 'fields' not in response:
        return
    for field, raw in response['fields'].items():
        if raw is None or raw == [] or raw == '':
            continue
        value = {'field': field, 'raw': raw, 'normalized': raw,
                 'source': response['source'], 'upstream': response['upstream'],
                 'url': response['url'], 'retrieved_at': response['retrieved_at'], 'validation': 'asserted'}
        if field == 'public':
            value['normalized'] = {'Adultes': 'adult', 'safe': 'all_ages', 'erotica': 'erotica',
                                   'pornographic': 'pornographic', 'suggestive': 'suggestive'}.get(str(raw))
        if value not in asset.setdefault('assertions', []):
            asset['assertions'].append(value)


def enrich(store: Catalog, asset_id: str, providers: Providers,
           references: list[tuple[str, str]]) -> dict[str, Any]:
    state = store.read()
    asset = state['assets'][asset_id]
    # All external work happens on the snapshot, outside catalogue locks.
    for provider, ref in references:
        add_assertions(asset, providers.source(provider, ref))
    embedded = asset.get('embedded', {})
    code = isbn13(str(embedded.get('ISBN', embedded.get('GTIN', ''))))
    candidates = []
    title = str(embedded.get('Title', ''))
    if title:
        asset['discovery'] = providers.discover(title)
    if code:
        response = providers.edition(code)
        asset.setdefault('source_attempts', []).append({k: v for k, v in response.items() if k != 'body'})
        if response['status'] == 'ok':
            candidates.append(response['candidate'])
    result = identify(embedded, candidates)
    asset['identity_result'] = result
    if result['status'] == 'identified':
        candidate = result['candidate']
        wid = digest({'source': candidate['source'], 'work': candidate['work_ref']})
        eid = digest({'source': candidate['source'], 'edition': candidate['edition_ref']})
        existing = asset.get('identity')
        if existing and existing['edition'] != eid:
            asset['contested'] = True
        elif not existing:
            apply_effect(state, {'action': 'identify', 'target': asset_id, 'values': {
                'work_id': wid, 'edition_id': eid, 'work': {'title': candidate['title'], 'author': candidate['author']},
                'edition': {'language': candidate['language'], 'publisher': candidate['publisher'],
                            'date': candidate['date'], 'revision': candidate['revision'], 'evidence': result['evidence']}}})
            state['editions'][eid].setdefault('selected', asset['identity']['variant'])
    elif asset.get('identity') and candidates:
        asset['contested'] = True
    store.save(state, expected=state['revision'])
    return store.read()
