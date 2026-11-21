"""Private BDneX transport. Scores locate references; they never establish identity."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

from library.bdnex_protocol import PIN, MAX_BODY, MAX_RESPONSE, digest, validate_request, validate_candidates, valid_match_counts
from library.names import parse_name


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class BDneXClient:
    def __init__(self, endpoint: str, token: str, *, timeout: float = 5):
        parts = urlsplit(endpoint)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password
                or parts.path not in ('', '/') or parts.query or parts.fragment or not token):
            raise ValueError('Invalid private BDneX configuration')
        self.endpoint, self.token, self.timeout = endpoint.rstrip('/'), token, timeout
        self.retry_at = 0.0

    def match(self, asset: str, inputs: list[dict[str, Any]]) -> dict[str, Any]:
        result = self._match(asset, inputs)
        if result['status'] != 'ok' or result['candidates']:
            return result

        # A scan prefix can hide a candidate. Try its basename without the prefix
        # only after the original request is empty, within the three-name budget.
        paths = {item['path'] for item in inputs}
        variants: list[dict[str, Any]] = []
        for item in inputs:
            if len(inputs) + len(variants) == 3:
                break
            folder, separator, filename = item['path'].rpartition('/')
            path = folder + separator + re.sub(r'^zz(?=[a-zÀ-ÿ])', '', filename, flags=re.I)
            if path not in paths:
                variants.append({**item, 'path': path})
                paths.add(path)
        if not variants:
            return result

        fallback = self._match(asset, variants)
        receipt = {'original_request': result['request']}
        if 'match_counts' in result:
            receipt['original_match_counts'] = result['match_counts']
        fallback['prefix_fallback'] = receipt
        return fallback

    def _match(self, asset: str, inputs: list[dict[str, Any]]) -> dict[str, Any]:
        if time.monotonic() < self.retry_at:
            return {'status': 'unavailable', 'reason': 'BDneX cooldown', 'dependency': 'bdnex'}
        try:
            request = validate_request({'schema': 1, 'asset': asset, 'inputs': inputs})
            body = json.dumps(request).encode()
            if len(body) > MAX_BODY:
                raise ValueError('BDneX request too large')
        except ValueError:
            return {'status': 'unavailable', 'reason': 'BDneX request invalid', 'dependency': 'bdnex'}
        try:
            req = Request(self.endpoint + '/v1/match', data=body,
                headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            # Private credentials must never follow redirects or ambient proxy settings.
            with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('BDneX response too large')
            result = json.loads(raw)
            if (result.get('schema') != 1 or result.get('asset') != asset or result.get('request') != digest(request)
                    or result.get('engine') != PIN or not re.fullmatch('[a-f0-9]{64}', str(result.get('index', '')))):
                raise ValueError('Invalid BDneX binding')
            rows = validate_candidates(result['candidates'])
            if not valid_match_counts(result.get('match_counts'), len(rows)):
                result.pop('match_counts', None)
            for row in rows:
                row['provenance'] = {'engine': PIN, 'index': result['index'], 'method': 'bdnex-sitemap-1'}
            return {**result, 'status': 'ok'}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            from booksys_core.diagnostics import exception_details
            diagnostic: dict[str, Any] = {'exception_type': exception_details(exc)['exception_type']}
            if isinstance(exc, HTTPError):
                diagnostic['http_status'] = exc.code
                exc.close()
                if exc.code == 400:
                    return {'status': 'unavailable', 'reason': 'BDneX request invalid', 'dependency': 'bdnex', **diagnostic}
            self.retry_at = time.monotonic() + 60
            return {'status': 'unavailable', 'reason': 'BDneX unavailable or invalid response', 'dependency': 'bdnex', **diagnostic}


def configured_client() -> BDneXClient | None:
    endpoint = os.environ.get('BOOKSYS_BDNEX_URL', '')
    if not endpoint:
        return None
    # Misconfiguration is a deployment error, never silently disable an enabled provider.
    token = Path(os.environ['BOOKSYS_BDNEX_TOKEN_FILE']).read_text().strip()
    return BDneXClient(endpoint, token)


def inputs_for(state: dict[str, Any], key: str) -> list[dict[str, Any]]:
    asset = state['assets'][key]
    hints = {k: str(v) for k, v in asset.get('embedded', {}).items()
             if k in ('Web', 'Series', 'Number', 'Volume') and isinstance(v, (str, int))}
    inputs = []
    for oid in asset.get('origins', []):
        origin = state['origins'][oid]
        if origin.get('sha256') != key:
            raise ValueError('BDneX input is not bound to asset')
        name = origin.get('member') if origin.get('parent_origin') else origin.get('path')
        if isinstance(name, str):
            # Preserve a member's immediate folder, never expose host storage paths.
            parts = PurePosixPath(name).parts
            logical = '/'.join(parts[-2:])
            item = {'path': logical, 'embedded': hints}
            if item not in inputs:
                inputs.append(item)
    return inputs[:3]


def bookys_heading_input(state: dict[str, Any], key: str, original: dict[str, Any]) -> dict[str, Any] | None:
    """A single direct Bookys item's heading can supply a logical search name."""
    asset = state['assets'][key]
    origins = asset.get('origins', [])
    if len(origins) != 1:
        return None
    origin = state['origins'][origins[0]]
    source = origin.get('source', '')
    if (origin.get('parent_origin') or origin.get('bundle')
            or not re.fullmatch(r'https://www\d*\.bookys-ebooks\.com/bandes-dessinees/\d+-[^/?#]+', source)):
        return None
    hint = asset.get('captured_hints', {}).get(source)
    title = hint.get('title') if isinstance(hint, dict) else None
    if not isinstance(title, str):
        return None
    fields = parse_name(re.sub(r'[/\\\x00-\x1f\x7f]', ' ', title))
    if not fields.get('series') or not fields.get('number'):
        return None
    name = f"{fields['series']} - T{str(fields['number']).zfill(2)}"
    if fields.get('title'):
        name += f" - {fields['title']}"
    path = name + '.cbz'
    if path == PurePosixPath(original['path']).name:
        return None
    return {'path': path, 'embedded': original['embedded']}
