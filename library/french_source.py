"""Bounded read-only qualification transport for the Bedetheque parser.

No accounts, solver, media download, hoster or live catalogue access.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml
from library.primitives import LibraryError, atomic_yaml, now
from library.french_metadata import BASE, reference_url

AGENT = 'BooksysMetadataQualification/0.1'


@dataclass(frozen=True)
class ProviderPage:
    status: str
    url: str
    body: str = ''
    retrieved_at: str = ''
    reason: str = ''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def robots_allowed(body: str, url: str) -> bool:
    """Combine all wildcard groups; Python RobotFileParser ignores later groups.

    The current site has two '*' groups. Longest matching path wins, with Allow
    winning an exact tie. No named training-crawler identity is impersonated.
    """
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []; rules: list[tuple[str, str]] = []
    for raw in body.splitlines():
        line = raw.split('#', 1)[0].strip()
        if ':' not in line:
            continue
        key, value = (part.strip() for part in line.split(':', 1))
        key = key.lower()
        if key == 'user-agent':
            if rules:
                groups.append((agents, rules)); agents=[]; rules=[]
            agents.append(value.lower())
        elif key in ('allow', 'disallow') and agents and value:
            rules.append((key, value))
    groups.append((agents, rules))
    matching = [(names, entries) for names, entries in groups if any(
        name == '*' or name in AGENT.lower() for name in names)]
    specificity = max((max((len(name) for name in names if name != '*' and name in AGENT.lower()), default=0)
                       for names, _ in matching), default=0)
    path = urlsplit(url).path
    if urlsplit(url).query:
        path += '?' + urlsplit(url).query
    matches = []
    for names, entries in matching:
        level = max((len(name) for name in names if name != '*' and name in AGENT.lower()), default=0)
        if level != specificity:
            continue
        for kind, pattern in entries:
            expression = re.escape(pattern).replace(r'\*', '.*').replace(r'\$', '$')
            if re.match(expression, path):
                matches.append((len(pattern.replace('*', '')), kind == 'allow'))
    return max(matches)[1] if matches else True


class QualificationSource:
    """A cache and a cumulative request ledger belong to this isolated proof only."""
    def __init__(self, cache: Path, *, live: bool = False, max_requests: int = 50,
                 interval: float = 2):
        if not 0 <= max_requests <= 50 or interval < 1:
            raise LibraryError('Qualification requires budget <=50 and interval >=1 second')
        self.cache, self.live, self.max_requests, self.interval = cache, live, max_requests, interval
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.ledger = cache / 'request-ledger.yaml'
        self.last_request = 0.0
        self.policy: str | None = None

    def request_count(self) -> int:
        value = yaml.safe_load(self.ledger.read_text()) if self.ledger.exists() else {'requests': []}
        return len(value['requests'])

    def _load(self, url: str) -> ProviderPage | None:
        path = self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.yaml')
        if not path.exists():
            return None
        value = yaml.safe_load(path.read_text())
        if value.get('url') != url:
            raise LibraryError('Qualification cache URL mismatch')
        return ProviderPage(**value)

    def _request(self, url: str) -> ProviderPage:
        cached = self._load(url)
        if cached:
            return cached
        if not self.live:
            return ProviderPage('unavailable', url, reason='No retained response; live access disabled')
        if self.request_count() >= self.max_requests:
            return ProviderPage('budget_exhausted', url, reason='Cumulative qualification request budget exhausted')
        time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        ledger = yaml.safe_load(self.ledger.read_text()) if self.ledger.exists() else {'requests': []}
        ledger['requests'].append({'url': url, 'started_at': now()})
        atomic_yaml(self.ledger, ledger)
        self.last_request = time.monotonic()
        try:
            request = Request(url, headers={'User-Agent': AGENT, 'Accept': 'text/html,text/plain'})
            with build_opener(NoRedirect).open(request, timeout=20) as response:
                raw = response.read(2 * 1024**2 + 1)
                if len(raw) > 2 * 1024**2:
                    result = ProviderPage('unexpected_response', url, reason='Response exceeds 2 MiB bound')
                else:
                    result = ProviderPage('ok', url, raw.decode('utf-8'), now())
        except HTTPError as exc:
            result = ProviderPage('not_found' if exc.code == 404 else 'unavailable', url,
                                  retrieved_at=now(), reason='HTTP ' + str(exc.code))
        except (OSError, UnicodeError) as exc:
            result = ProviderPage('unavailable', url, retrieved_at=now(), reason=type(exc).__name__ + ': ' + str(exc))
        from dataclasses import asdict
        atomic_yaml(self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.yaml'), asdict(result))
        return result

    def get(self, url: str) -> ProviderPage:
        url = reference_url(url)
        if urlsplit(url).path == '/robots.txt':
            raise LibraryError('Use the transport-owned robots policy')
        if self.policy is None:
            policy = self._request(BASE + '/robots.txt')
            if policy.status != 'ok' or not policy.body.lower().find('user-agent:') >= 0:
                return ProviderPage(policy.status if policy.status != 'ok' else 'unexpected_response', url,
                                    reason='Robots policy unavailable or unexpected')
            self.policy = policy.body
        if not robots_allowed(self.policy, url):
            return ProviderPage('robots_disallowed', url, reason='Robots policy disallows this reference path')
        # Do not fetch media even when robots changes; T1 only needs reference HTML.
        if re.search(r'\.(?:png|jpe?g|gif|webp|pdf|cb[rz])$', urlsplit(url).path, re.I):
            return ProviderPage('robots_disallowed', url, reason='Media downloads are outside qualification')
        return self._request(url)
