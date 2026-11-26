"""Single request at a time. This container receives labels, never comic archives."""
from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable

from library.bdnex_protocol import (
    FAMILY_PATH, FAMILY_SCHEMA, PIN, MAX_BODY, MAX_RESPONSE, digest, validate_request,
    validate_candidates, validate_decision, validate_family_request, validate_family_result, valid_match_counts,
)


def handler(token: str, match: Callable[[dict[str, Any]], dict[str, Any]],
            lookup_family: Callable[[dict[str, str]], dict[str, Any]] | None = None) -> type[BaseHTTPRequestHandler]:
    if not token:
        raise ValueError('An authentication token is required')

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # No request bodies, logical names or credentials in access logs.

        def reply(self, code: int, value: dict[str, Any]) -> None:
            raw = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self.reply(200 if self.path == '/healthz' else 404, {'ready': self.path == '/healthz'})

        def do_POST(self) -> None:
            family = lookup_family if self.path == FAMILY_PATH else None
            if self.path != '/v1/match' and family is None:
                self.reply(404, {})
                return
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.reply(401, {})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Invalid body length')
                body = json.loads(self.rfile.read(length))
                request = validate_family_request(body) if family else validate_request(body)
            except (ValueError, KeyError, TypeError):
                self.reply(400, {})
                return
            try:
                if family:
                    result = family(request)
                    validate_family_result(result, request)
                    payload = {**result, 'schema': FAMILY_SCHEMA, 'request': digest(body), 'engine': PIN}
                else:
                    result = match(request)
                    rows = validate_candidates(result['candidates'])
                    if 'decision' in result:
                        validate_decision(result['decision'], rows)
                    payload = {**result, 'schema': 1, 'asset': request['asset'], 'request': digest(request), 'engine': PIN}
                    if not valid_match_counts(payload.get('match_counts'), len(rows)):
                        payload.pop('match_counts', None)
                if len(json.dumps(payload).encode()) > MAX_RESPONSE:
                    raise ValueError('Response too large')
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                self.reply(503, {})
                return
            self.reply(200, payload)

    return Handler
