"""Controller-owned fixed public views. No arbitrary path or adapter operation."""
from __future__ import annotations

import base64
from datetime import datetime
import json
from pathlib import Path
import re
import secrets
from typing import Any

from library.catalog import Catalog
from library.primitives import LibraryError, now
from library.views import read_yaml, build_projection, fiche_html
from library.resolution import Resolution, case_view
from booksys_core.diagnostics import safe_text


class PublicAPI:
    def __init__(self, root: Path, *, host: str, reception: Path,
                 diagnostic_reader: Any = None, relationships: Any = None, public_origin: str | None = None):
        self.root, self.host = root, host
        from library.http import public_origins
        public_origins(host, public_origin)  # Reject malformed configuration at startup.
        self.public_origin = public_origin
        self.catalog = Catalog(root / 'state')
        self.resolution = Resolution(self.catalog, reception)
        self.token = secrets.token_urlsafe(32)
        self.diagnostic_reader, self.relationships = diagnostic_reader, relationships
        self.captured: dict[str, Any] = {}
        self.recovery: dict[str, Any] = {}
        from library.activity import PublicSnapshot
        self.activity = PublicSnapshot(root)

    def cycle_view(self) -> dict[str, Any]:
        from library.operational_state import StatusObservations
        raw = StatusObservations(self.catalog.backend()._database).cycle()
        names = ('status', 'stage', 'stage_started_at', 'started_at', 'finished_at', 'duration_seconds',
                 'next_cycle_at', 'reason', 'error_source', 'backlog', 'intake_backlog', 'assets', 'origins', 'published')
        return {'recovery': self.recovery, 'diagnostics': self.diagnostic_health(), **{key: safe_text(value) if isinstance(value, str) else value for key, value in raw.items()
                if key in names and (isinstance(value, str) or type(value) in (int, float) or value is None)}}

    def diagnostic_health(self) -> dict[str, int]:
        from booksys_core.diagnostics import degradation
        from activity import degradation as activity_degradation
        return {**degradation(), 'activity_dropped_count': activity_degradation()}

    def validate(self, action: str, request: dict[str, Any]) -> None:
        from library.http import public_origins
        origins = public_origins(self.host, self.public_origin)
        if (set(request) != {'payload', 'host', 'origin', 'token'}
                or any(not isinstance(request[key], str) for key in ('host', 'origin', 'token'))
                or request['host'] not in {origin.split('://', 1)[1] for origin in origins}):
            raise LibraryError('Invalid public request envelope')
        payload = request['payload']
        if not isinstance(payload, dict):
            raise LibraryError('Object required')
        write_actions = ('page', 'search', 'case', 'preview', 'confirm', 'confirm-work')
        if action in write_actions and (request['origin'] not in origins
                                       or request['origin'].split('://', 1)[-1] != request['host']
                                       or not request['token'].isascii()
                                       or not secrets.compare_digest(request['token'], self.token)):
            raise LibraryError('Forbidden origin or token')
    def invoke(self, action: str, request: dict[str, Any]) -> Any:
        result = self._invoke(action, request)
        # Indexed library/status rows are fixed DTOs filtered by operator_row
        # before persistence. Their envelope/cycle fields are controlled here.
        # Do not repeat a recursive free-form filter over every stored scalar.
        if action in ('proof', 'cover', 'fiche', 'library', 'status', 'activity'):
            return result
        from library.presentation import public_value
        return public_value(result)

    def _invoke(self, action: str, request: dict[str, Any]) -> Any:
        self.validate(action, request)
        payload = request['payload']
        if action == 'activity' and not payload:
            return self.activity.response()
        backend = self.catalog.backend()
        if action == 'token' and not payload:
            return {'token': self.token}
        if action == 'library' and backend is not None:
            if 'source' in payload:
                if set(payload) - {'source', 'after', 'limit'}:
                    raise LibraryError('Invalid child query')
                return backend.operator_children(**payload)
            if set(payload) - {'filters', 'search', 'sort', 'cursor', 'limit', 'descending', 'page'}:
                raise LibraryError('Invalid library query')
            return {**backend.operator_page(**payload), 'cycle': self.cycle_view()}
        if action == 'status' and not payload and backend is not None:
            with backend._database.connection() as db:
                revision, _ = backend.check(db)
                estimate = db.execute('SELECT coalesce(sum(octet_length(payload)+1),0) FROM operator_rows').fetchone()
                if estimate is None:
                    raise LibraryError('Operator observation unavailable')
                estimated=estimate[0]
                if estimated > 16 * 1024**2 - 4096:
                    raise LibraryError('Compatibility view exceeds16MiB')
                rows = [row[0] for row in db.execute('SELECT payload FROM operator_rows ORDER BY id')]
                source_row = db.execute('SELECT count(DISTINCT source_ref) FROM operator_rows').fetchone()
                source_count=source_row[0] if source_row else 0
                counts = backend.compatibility_counts(db)
            value = {'schema': 1, 'generated_at': now(), 'catalogue_revision': revision,
                     'cycle': self.cycle_view(),
                     'counts': {'sources': source_count, 'origins': len(rows), **counts},
                     'source': 'saved queue and catalogue snapshots; not live pyLoad'}
            from library.ipc import EncodedJSON
            header = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            return EncodedJSON((header[:-1] + ',"rows":[' + ','.join(rows) + ']}').encode())
        if action in ('fiche', 'cover', 'proof'):
            if set(payload) != {'id'} or not isinstance(payload['id'], str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', payload['id']):
                raise LibraryError('Invalid public identity')
            ident = payload['id']
            if action == 'proof':
                return {'png': self.resolution.load_piece(ident)['png']}
            if action == 'cover':
                asset = self.catalog.get('assets', ident) or {}
                raw = base64.b64decode(asset.get('cover', ''), validate=True)
                if len(raw) > 1024**2 or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise LibraryError('Cover unavailable')
                return {'png': base64.b64encode(raw).decode()}
            if backend is not None:
                origin = backend.get('origins', ident, media=False)
                if origin is None:
                    raise LibraryError('Fiche unavailable')
                state = backend.resolution_state(origin['sha256']) if origin.get('sha256') and backend.get(
                    'assets', origin['sha256'], media=False) is not None else backend.working_set('resolve', {'origins': [ident]})
                projection = build_projection(self.root, state=state, captured=self.captured, write_covers=False)
            else:
                projection = read_yaml(self.root / 'evidence/dashboard.yaml')
            fiche = projection.get('fiches', {}).get(ident)
            if fiche is None:
                raise LibraryError('Fiche unavailable')
            from library.presentation import public_value
            return {'html': fiche_html(public_value(fiche))}
        if action == 'diagnostics' and self.diagnostic_reader is not None:
            from library.diagnostic_reader import Filter
            if set(payload) - {'filter', 'cursor'} or not isinstance(payload.get('filter'), dict):
                raise LibraryError('Invalid diagnostic query')
            return self.diagnostic_reader.query(Filter(**payload['filter']), cursor=payload.get('cursor'))
        if action == 'relationships' and self.relationships is not None:
            return self.relationships.resolve(**payload)
        if action == 'case' and set(payload) in ({'origin'}, {'asset'}):
            key = payload.get('asset') or (self.catalog.get('origins', payload['origin']) or {})['sha256']
            return case_view(self.resolution, key)
        if action == 'page' and set(payload) == {'asset', 'page'}:
            return self.resolution.page(payload['asset'], payload['page'])
        if action == 'preview' and set(payload) == {'asset', 'values'}:
            return self.resolution.preview(payload['asset'], payload['values'])
        if action == 'search' and set(payload) == {'query'}:
            return self.resolution.search(payload['query'])
        if action == 'confirm':
            return self.resolution.confirm(payload)
        if action == 'confirm-work':
            from library.work_resolution import confirm_work
            return confirm_work(self.resolution, payload)
        raise LibraryError('Unsupported public action or fields')
