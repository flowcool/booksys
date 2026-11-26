"""Look up and review one MangaUpdates reference per WEBTOONS_LISTS title."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml

from booksys_core.browser_outcomes import retry_after
from library.primitives import LibraryError, atomic_yaml, lock, now
from library.webtoons import series_key

STATE_FILE = 'webtoon-series.yaml'
API = 'https://api.mangaupdates.com/v1/series/'
REFERENCE_URL = re.compile(r'https://www\.mangaupdates\.com/series/[a-z0-9]+(?:/[a-z0-9_-]+)?/?', re.I)
REQUEST_LIMIT = 2 * 1024**2
STATE_LIMIT = 4 * 1024**2
SeriesRequest = Callable[[str, dict[str, str] | None], dict[str, Any]]


def text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 1024 or re.search(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]', value):
        return ''
    return ' '.join(value.split())


def candidate(record: dict[str, Any], hit_title: Any = None) -> dict[str, Any]:
    mu_id, title, url, kind = record.get('series_id'), text(record.get('title')), text(record.get('url')), text(record.get('type'))
    if type(mu_id) is not int or mu_id <= 0 or not title or not kind or not REFERENCE_URL.fullmatch(url):
        raise LibraryError('Invalid MangaUpdates series record')
    aliases = [text(alias.get('title')) for alias in record.get('associated', []) if isinstance(alias, dict)]
    authors = [{'name': text(author.get('name')), 'type': text(author.get('type'))}
               for author in record.get('authors', []) if isinstance(author, dict) and text(author.get('name'))]
    return {'mu_id': mu_id, 'title': title, 'url': url, 'type': kind, 'authors': authors,
            'aliases': list(dict.fromkeys(alias for alias in [text(hit_title), *aliases] if alias)),
            'details': 'authors' in record}


def exact_manhwa(key: str, entry: dict[str, Any]) -> bool:
    return bool(key and entry['type'] == 'Manhwa' and any(
        series_key(title) == key for title in (entry['title'], *entry['aliases'])))


def with_decision(entry: dict[str, Any], chosen: dict[str, Any] | None, decision: str, operator: str) -> dict[str, Any]:
    fields: dict[str, Any] = {'mu_id': None, 'title': None, 'url': None, 'type': None, 'authors': []}
    if chosen is not None:
        fields = {field: chosen[field] for field in fields}
    return {**entry, **fields, 'decision': decision, 'decided_by': operator, 'decided_at': now()}


def decide_series(title: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Use the returned page, including the associated title matched by search."""
    key = series_key(title)
    candidates = [candidate(hit['record'], hit.get('hit_title')) for hit in hits]
    matches = {entry['mu_id']: entry for entry in candidates if exact_manhwa(key, entry)}
    chosen = next(iter(matches.values())) if len(matches) == 1 else None
    top = {entry['mu_id']: entry for entry in candidates[:5]}
    top.update(matches)
    return with_decision({'index_title': title, 'candidates': list(top.values())}, chosen,
                         'accepted' if chosen is not None else 'review', 'mangaupdates')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def request_series(path: str, payload: dict[str, str] | None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    request = Request(API + path, data=data, headers={'Accept': 'application/json',
        'Content-Type': 'application/json', 'User-Agent': 'Booksys/0.1 (personal metadata catalogue)'})
    opener = build_opener(NoRedirect)
    delay = 1.0
    for attempt in range(3):
        # The caller holds the series state lock. Waiting before each start
        # also spaces requests across concurrent or consecutive CLI invocations.
        time.sleep(delay)
        try:
            with opener.open(request, timeout=10) as response:
                body = response.read(REQUEST_LIMIT + 1)
        except HTTPError as exc:
            exc.close()
            if attempt == 2 or (exc.code != 429 and not 500 <= exc.code < 600):
                raise LibraryError(f'MangaUpdates HTTP {exc.code}') from exc
            kind, wait = retry_after(exc.headers.get('Retry-After'), received_at=time.time())
            if kind in ('invalid', 'unrepresentable') or (wait is not None and wait > 30):
                raise LibraryError(f'MangaUpdates HTTP {exc.code}; retry delay unavailable or too long') from exc
            delay = max(2.0 ** attempt, wait or 0)
        except OSError as exc:
            if attempt == 2:
                raise LibraryError('MangaUpdates request failed after three attempts') from exc
            delay = 2.0 ** attempt
        else:
            if len(body) > REQUEST_LIMIT:
                raise LibraryError('MangaUpdates response exceeds the size limit')
            value = json.loads(body)
            if not isinstance(value, dict):
                raise LibraryError('Invalid MangaUpdates response')
            return value
    raise AssertionError('Request loop did not return or raise')


def read_references(directory: Path) -> dict[str, dict[str, Any]]:
    try:
        with (directory / STATE_FILE).open('rb') as stream:
            body = stream.read(STATE_LIMIT + 1)
    except FileNotFoundError:
        return {}
    if len(body) > STATE_LIMIT:
        raise LibraryError('Webtoon series reference state exceeds the size limit')
    try:
        value = yaml.load(body, Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
    except yaml.YAMLError as exc:
        raise LibraryError('Invalid webtoon series reference YAML') from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(entry, dict)
            for key, entry in value.items()):
        raise LibraryError('Webtoon series references must be keyed by index title')
    return value


def hydrate(entry: dict[str, Any], chosen: dict[str, Any], request: SeriesRequest) -> dict[str, Any]:
    if chosen['details']:
        return chosen
    details = candidate(request(str(chosen['mu_id']), None))
    if details['mu_id'] != chosen['mu_id']:
        raise LibraryError('MangaUpdates series detail ID differs from the candidate')
    entry['candidates'] = [details if row['mu_id'] == details['mu_id'] else row for row in entry['candidates']]
    return details


def lookup(directory: Path, titles: Iterable[str], *, request: SeriesRequest = request_series) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    with lock(directory / 'webtoon-series.lock'):
        references = read_references(directory)
        for raw_title in titles:
            title = text(raw_title)
            key = series_key(title)
            if not key:
                raise LibraryError('Expected a non-empty WEBTOONS_LISTS title')
            if key not in references:
                hits = request('search', {'search': title}).get('results')
                if not isinstance(hits, list):
                    raise LibraryError('Invalid MangaUpdates search results')
                entry = decide_series(title, hits)
                if entry['decision'] == 'accepted':
                    chosen = next(row for row in entry['candidates'] if row['mu_id'] == entry['mu_id'])
                    details = hydrate(entry, chosen, request)
                    entry = (with_decision(entry, details, 'accepted', 'mangaupdates') if exact_manhwa(key, details)
                             else with_decision(entry, None, 'review', 'mangaupdates'))
                references[key] = entry
                atomic_yaml(directory / STATE_FILE, references)
            results[key] = references[key]
    return results


def decide_candidate(directory: Path, title: str, mu_id: int, decision: str, operator: str) -> dict[str, Any]:
    key = series_key(title)
    with lock(directory / 'webtoon-series.lock'):
        references = read_references(directory)
        entry = references.get(key, {})
        chosen = next((row for row in entry.get('candidates', []) if row['mu_id'] == mu_id), None)
        if chosen is None:
            raise LibraryError('Candidate is absent for this WEBTOONS_LISTS title')
        if decision == 'accepted':
            chosen = hydrate(entry, chosen, request_series)
        result = with_decision(entry, chosen, decision, operator)
        references[key] = result
        atomic_yaml(directory / STATE_FILE, references)
        return result


def comicinfo_reference(reference: dict[str, Any]) -> dict[str, Any]:
    if reference.get('decision') != 'accepted':
        return {}
    url = text(reference.get('url'))
    title = text(reference.get('title'))
    if not title or not REFERENCE_URL.fullmatch(url):
        raise LibraryError('Invalid accepted MangaUpdates reference')
    authors = reference.get('authors', [])
    contributors = {role: list(dict.fromkeys(author['name'] for author in authors
        if author['type'] == kind)) for role, kind in (('writer', 'Author'), ('penciller', 'Artist'))}
    return {'alternate_series': title, 'url': url, 'manga': 'Yes', 'contributors': contributors}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('lookup').add_argument('title', nargs='+', help='WEBTOONS_LISTS index title')
    commands.add_parser('review').add_argument('title', nargs='?', help='Show one title or list all pending reviews')
    for action in ('accept', 'reject'):
        command = commands.add_parser(action)
        command.add_argument('title')
        command.add_argument('mu_id', type=int)
        command.add_argument('--by', required=True, help='Operator name to record with the decision')
    args = parser.parse_args(argv)
    try:
        if args.command == 'lookup':
            results = lookup(args.state_dir, args.title)
        elif args.command == 'review':
            results = {key: entry for key, entry in read_references(args.state_dir).items()
                       if (key == series_key(args.title) if args.title is not None else entry['decision'] == 'review')}
        else:
            operator = text(args.by)
            if not operator:
                raise LibraryError('Expected an operator name')
            decision = 'accepted' if args.command == 'accept' else 'rejected'
            results = decide_candidate(args.state_dir, args.title, args.mu_id, decision, operator)
        print(json.dumps(results, indent=2, ensure_ascii=False))
    except (LibraryError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f'Webtoon series reference: {exc}\n')


if __name__ == '__main__':
    main()
