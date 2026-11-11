"""HTTP routing. Presentation remains in views; business actions are explicit POSTs."""
from __future__ import annotations
import base64
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
from typing import Any
from library.views import PAGE, read_yaml, fiche_html
from library.presentation import fields


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


def handler(root: Path, *, resolution: Any = None, api: Any = None) -> type[BaseHTTPRequestHandler]:
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

        def origins(self) -> tuple[str, ...]:
            address = str(self.server.server_address[0])  # type: ignore[index]
            port = self.server.server_port  # type: ignore[attr-defined]
            host = getattr(self.server, 'public_host', str(address) + ':' + str(port))
            return public_origins(host, getattr(self.server, 'public_origin', None))

        def allowed_host(self) -> bool:
            return (len(self.headers.get_all('Host', [])) == 1
                    and all(len(self.headers.get_all(name, [])) <= 1 for name in ('Origin', 'X-CSRF-Token'))
                    and self.headers.get('Host', '') in {origin.split('://', 1)[1] for origin in self.origins()})

        def mediated(self, action: str, payload: dict[str, Any]) -> Any:
            from library.ipc import IPCError
            return api.call(action, {'payload': payload, 'host': self.headers.get('Host', ''),
                'origin': self.headers.get('Origin', ''), 'token': self.headers.get('X-CSRF-Token', '')})

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
            if route.path in ('/api/status', '/api/activity', '/api/resolution/token') and not query:
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
            elif re.fullmatch(r'/api/resolution/proof/[a-f0-9]{64}', route.path) and not query:
                action, payload = 'proof', {'id': route.path.rsplit('/', 1)[1]}
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
                if action in ('status', 'activity') and isinstance(result, EncodedJSON):
                    self.respond(200, result.body, 'application/json; charset=utf-8')
                elif action in ('cover', 'proof'):
                    self.respond(200, base64.b64decode(result['png'], validate=True), 'image/png')
                elif action == 'fiche':
                    from library.resolution_ui import PANEL
                    self.respond(200, (result['html'] + PANEL).encode(), 'text/html; charset=utf-8')
                else:
                    status = 429 if result.get('state') == 'busy' else 200
                    self.respond(status, json.dumps(result).encode(), 'application/json; charset=utf-8')
            except IPCError as exc:
                code = 429 if str(exc).startswith('busy:') else 409 if action in ('case', 'preview') else 503
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
            if self.path == '/api/activity':
                from library.activity import load_public
                self.respond(200, json.dumps(load_public(root)).encode(), 'application/json; charset=utf-8')
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
            if any(len(self.headers.get_all(name, [])) > 1 for name in ('Origin', 'X-CSRF-Token')):
                self.respond(403, b'Duplicate security header', 'text/plain'); return
            if api is not None:
                if not self.allowed_host():
                    self.respond(403, b'Invalid Host', 'text/plain'); return
                from library.ipc import IPCError
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if self.headers.get('Content-Type') != 'application/json' or not 0 < length <= 65536:
                        raise ValueError('Bounded JSON required')
                    action = self.path.removeprefix('/api/resolution/')
                    if not self.path.startswith('/api/resolution/') or action not in ('case', 'page', 'preview', 'confirm', 'confirm-work', 'search'):
                        raise ValueError('Unsupported action')
                    from library.ipc import decode
                    try:
                        value = decode(self.rfile.read(length))
                    except ValueError as exc:
                        raise ValueError('Invalid JSON') from exc
                    if not isinstance(value, dict):
                        raise ValueError('Object required')
                    result = self.mediated(action, value)
                    self.respond(200, json.dumps(result).encode(), 'application/json')
                except IPCError as exc:
                    self.respond(429 if str(exc).startswith('busy:') else 409,
                                 json.dumps({'reason': str(exc)}).encode(), 'application/json')
                except (ValueError, TypeError):
                    self.respond(400, b'Invalid action', 'text/plain')
                return
            if resolution is None:
                self.rejected_method()
                return
            from library.primitives import LibraryError
            if (not self.allowed_host() or self.headers.get('Origin') not in self.origins()
                    or self.headers.get('Origin', '').split('://', 1)[-1] != self.headers.get('Host', '')
                    or not self.headers.get('X-CSRF-Token', '').isascii()
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
                    key = value.get('asset') or (resolution.store.get('origins', value['origin']) or {})['sha256']
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
