"""HTTP routing. Presentation remains in views; business actions are explicit POSTs."""
from __future__ import annotations
import base64
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
from typing import Any
from library.views import PAGE, read_yaml, fiche_html


def public_origins(host: str, public_origin: str | None = None) -> tuple[str, ...]:
    """Keep the local HTTP endpoint and optionally one exact proxy origin."""
    from urllib.parse import urlsplit
    origins = ('http://' + host,)
    if not public_origin:
        return origins
    parsed = urlsplit(public_origin)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or public_origin != parsed.scheme + '://' + parsed.netloc
            or not re.fullmatch(r'(?:[a-z0-9]+(?:[.-][a-z0-9]+)*|\[[0-9a-f:]+\])(?::[1-9][0-9]{0,4})?', parsed.netloc)
            or parsed.port == 0):
        raise ValueError('Expected an exact canonical HTTP or HTTPS public origin')
    return origins if public_origin in origins else (*origins, public_origin)


def handler(root: Path, *, api: Any = None) -> type[BaseHTTPRequestHandler]:

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

        def origins(self) -> tuple[str, ...]:
            return public_origins(api.host, api.public_origin)

        def allowed_host(self) -> bool:
            return (len(self.headers.get_all('Host', [])) == 1
                    and all(len(self.headers.get_all(name, [])) <= 1 for name in ('Origin', 'X-CSRF-Token'))
                    and self.headers.get('Host', '') in {origin.split('://', 1)[1] for origin in self.origins()})

        def mediated(self, action: str, payload: dict[str, Any]) -> Any:
            from library.ipc import IPCError
            try:
                return api.invoke(action, {'payload': payload, 'host': self.headers.get('Host', ''),
                    'origin': self.headers.get('Origin', ''), 'token': self.headers.get('X-CSRF-Token', '')})
            except Exception as exc:
                raise IPCError('Action unavailable; inspect filtered diagnostics') from exc

        def mediated_get(self) -> None:
            from urllib.parse import urlsplit, parse_qs
            from library.ipc import IPCError, EncodedJSON
            route = urlsplit(self.path)
            if len(self.path) > 8192:
                self.respond(400, b'Query too large', 'text/plain'); return
            query = parse_qs(route.query, keep_blank_values=True, max_num_fields=16)
            if any(len(values) != 1 for values in query.values()):
                raise ValueError('Duplicate query fields')
            if route.path == '/':
                self.respond(200, PAGE.encode(), 'text/html; charset=utf-8'); return
            action = ''
            payload: dict[str, Any] = {}
            if route.path == '/api/status' and not query:
                action = route.path.rsplit('/', 1)[1]
            elif route.path == '/api/library':
                allowed = {'search', 'sort', 'cursor', 'limit', 'descending', 'page', 'source', 'after', 'status', 'transfer', 'intake', 'identity', 'publication'}
                if set(query) - allowed:
                    raise ValueError('Unsupported query')
                if 'source' in query:
                    result = self.mediated('library', {key: int(values[0]) if key == 'limit' else values[0] for key, values in query.items()})
                    self.respond(200, json.dumps(result).encode(), 'application/json'); return
                payload = {key: values[0] for key, values in query.items() if key in ('search', 'sort', 'cursor')}
                payload['filters'] = {key: values[0] for key, values in query.items() if key not in ('search', 'sort', 'cursor', 'limit', 'descending', 'page')}
                if 'descending' in query:
                    if query['descending'][0] not in ('true', 'false'):
                        raise ValueError('Invalid sort direction')
                    payload['descending'] = query['descending'][0] == 'true'
                if 'page' in query:
                    payload['page'] = int(query['page'][0])
                if 'limit' in query:
                    payload['limit'] = int(query['limit'][0])
                action = 'library'
            elif re.fullmatch(r'/fiche/[a-zA-Z0-9_-]{1,64}', route.path) and not query:
                action, payload = 'fiche', {'id': route.path.rsplit('/', 1)[1]}
            elif re.fullmatch(r'/cover/[a-f0-9]{64}\.png', route.path) and not query:
                action, payload = 'cover', {'id': route.path.rsplit('/', 1)[1][:-4]}
            elif route.path == '/api/relationships':
                if set(query) - {'kind', 'id', 'limit', 'cursor'} or not {'kind', 'id'} <= set(query):
                    raise ValueError('Typed relationship reference required')
                action = 'relationships'
                payload = {'typed_ref': {'kind': query['kind'][0], 'id': query['id'][0]}}
                if 'limit' in query:
                    payload['limit'] = int(query['limit'][0])
                if 'cursor' in query:
                    payload['cursor'] = query['cursor'][0]
            elif route.path == '/api/diagnostics':
                from library.diagnostic_cli import utc_epoch
                allowed = {'from', 'to', 'service', 'stage', 'reason', 'ref_kind', 'ref', 'cycle_id', 'cursor'}
                if set(query) - allowed or not {'from', 'to'} <= set(query):
                    raise ValueError('Bounded diagnostic window required')
                values = {key: value[0] for key, value in query.items()}
                payload = {'filter': {key: value for key, value in values.items() if key not in ('from', 'to', 'cursor')}}
                payload['filter'].update(start=utc_epoch(values['from']), end=utc_epoch(values['to']))
                if 'cursor' in values:
                    payload['cursor'] = values['cursor']
                action = 'diagnostics'
            else:
                self.respond(404, b'Not found', 'text/plain'); return
            try:
                result = self.mediated(action, payload)
                if action == 'status' and isinstance(result, EncodedJSON):
                    self.respond(200, result.body, 'application/json; charset=utf-8')
                elif action == 'cover':
                    self.respond(200, base64.b64decode(result['png'], validate=True), 'image/png')
                elif action == 'fiche':
                    self.respond(200, result['html'].encode(), 'text/html; charset=utf-8')
                else:
                    status = 429 if result.get('state') == 'busy' else 200
                    self.respond(status, json.dumps(result).encode(), 'application/json; charset=utf-8')
            except IPCError as exc:
                code = 429 if str(exc).startswith('busy:') else 503
                self.respond(code, json.dumps({'reason': str(exc)}).encode(), 'application/json')

        def do_GET(self) -> None:
            if api is not None:
                if not self.allowed_host():
                    self.respond(403, b'Invalid Host', 'text/plain'); return
                try:
                    self.mediated_get()
                except (ValueError, TypeError, KeyError):
                    self.respond(400, b'Invalid bounded query', 'text/plain')
                return
            if self.path == '/':
                self.respond(200, PAGE.encode(), 'text/html; charset=utf-8')
                return
            projection = read_yaml(root / 'evidence/dashboard.yaml')
            if self.path == '/api/status':
                if projection:
                    from library.operational_state import StatusObservations
                    cycle = StatusObservations(__import__('library.postgres',fromlist=['Database']).Database.configured(root/'state')).cycle()
                    projection['cycle'] = {k: cycle[k] for k in ('status', 'stage', 'stage_started_at',
                        'started_at', 'finished_at', 'duration_seconds', 'next_cycle_at', 'reason',
                        'error_source', 'backlog', 'intake_backlog', 'assets', 'origins', 'published') if k in cycle}
                self.respond(200 if projection else 503, json.dumps({k: v for k, v in projection.items()
                    if k != 'fiches'}).encode(), 'application/json; charset=utf-8')
                return
            if re.fullmatch(r'/fiche/[a-zA-Z0-9_-]{1,64}', self.path):
                fiche = projection.get('fiches', {}).get(self.path.removeprefix('/fiche/'))
                if fiche:
                    body = fiche_html(fiche)
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

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = rejected_method

        def log_message(self, *_: Any) -> None:
            pass
    return Handler
