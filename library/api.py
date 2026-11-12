"""Controller-owned fixed public views. No arbitrary path or adapter operation."""
from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
import re
import secrets
from typing import Any

from library.catalog import Catalog
from library.primitives import LibraryError, now
from library.views import read_yaml, build_projection, fiche_html
from booksys_core.diagnostics import safe_text


class PublicAPI:
    def __init__(self, root: Path, *, host: str, reception: Path,
                 diagnostic_reader: Any = None, relationships: Any = None, public_origin: str | None = None):
        self.root, self.host = root, host
        from library.http import public_origins
        public_origins(host, public_origin)  # Reject malformed configuration at startup.
        self.public_origin = public_origin
        self.catalog = Catalog(root / 'state')
        self.diagnostic_reader, self.relationships = diagnostic_reader, relationships
        self.captured: dict[str, Any] = {}
        self.recovery: dict[str, Any] = {}

    def cycle_view(self) -> dict[str, Any]:
        from library.operational_state import StatusObservations
        raw = StatusObservations(self.catalog.backend()._database).cycle()
        names = ('status', 'stage', 'stage_started_at', 'started_at', 'finished_at', 'duration_seconds',
                 'next_cycle_at', 'reason', 'error_source', 'backlog', 'intake_backlog', 'assets', 'origins', 'published')
        return {'recovery': self.recovery, 'diagnostics': self.diagnostic_health(), **{key: safe_text(value) if isinstance(value, str) else value for key, value in raw.items()
                if key in names and (isinstance(value, str) or type(value) in (int, float) or value is None)}}

    def diagnostic_health(self) -> dict[str, int]:
        from booksys_core.diagnostics import degradation
        return degradation()

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
    def invoke(self, action: str, request: dict[str, Any]) -> Any:
        result = self._invoke(action, request)
        # Library/status rows are fixed DTOs filtered by operator_row.
        if action in ('cover', 'fiche', 'library', 'status'):
            return result
        from library.presentation import public_value
        return public_value(result)

    def _invoke(self, action: str, request: dict[str, Any]) -> Any:
        self.validate(action, request)
        payload = request['payload']
        backend = self.catalog.backend()
        if action == 'library' and backend is not None:
            if 'source' in payload:
                if set(payload) - {'source', 'after', 'limit'}:
                    raise LibraryError('Invalid child query')
                return backend.operator_children(**payload)
            if set(payload) - {'filters', 'search', 'sort', 'cursor', 'limit', 'descending', 'page'}:
                raise LibraryError('Invalid library query')
            return {**backend.operator_page(**payload), 'cycle': self.cycle_view()}
        if action == 'status' and not payload and backend is not None:
            from library.projection import operator_row
            with backend._database.connection() as db:
                revision, _ = backend.check(db)
                rows = [operator_row(oid, backend.restore(origin, media=False),
                            backend.restore(asset, media=False) if asset else {}, {})
                        for oid, origin, asset in db.execute('SELECT o.id,o.payload,a.payload FROM origins o '
                            'LEFT JOIN assets a ON a.id=o.asset_id ORDER BY o.id')]
                source_count = db.execute('SELECT count(DISTINCT source_ref) FROM origins').fetchall()[0][0]
                classified, holding = db.execute("SELECT count(*) FILTER (WHERE admission_status='classified'),"
                    "count(*) FILTER (WHERE admission_status='holding') FROM assets").fetchall()[0]
            return {'schema': 1, 'generated_at': now(), 'catalogue_revision': revision,
                    'cycle': self.cycle_view(), 'rows': rows,
                    'counts': {'sources': source_count, 'origins': len(rows),
                               'classified_assets': classified, 'holding_assets': holding},
                    'source': 'saved queue and catalogue snapshots; not live pyLoad'}
        if action in ('fiche', 'cover'):
            if set(payload) != {'id'} or not isinstance(payload['id'], str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', payload['id']):
                raise LibraryError('Invalid public identity')
            ident = payload['id']
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
                state = backend.working_set('identify', {'origins': [ident]})
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
        raise LibraryError('Unsupported public action or fields')
