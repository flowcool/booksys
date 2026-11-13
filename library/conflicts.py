from __future__ import annotations

from library.postgres import DeadlineConnection
from typing import Any, TYPE_CHECKING

from library.codec import decode
from library.primitives import TABLES, LibraryError, digest
if TYPE_CHECKING:
    from library.store import Store, Change


def snapshot(store: Store, db: DeadlineConnection, action: str, refs: dict[str, list[str]],
             claims: dict[str, list[str]], *, media: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    state = store.working_set(action, refs, media=False, _db=db)
    covers: dict[str, dict[str, str]] = {}
    payloads: dict[str, dict[str, str]] = {}
    for kind in TABLES:
        for ident in state[kind]:
            row = db.execute(f'SELECT payload FROM {kind} WHERE id=%s',(ident,)).fetchone()
            if row:
                payloads.setdefault(kind,{})[ident] = row[0]
                envelope = decode(row[0])
                if envelope.get('cover'):
                    covers.setdefault(kind,{})[ident] = envelope['cover']
    claims = {field: list(values) for field, values in claims.items()}
    from library.bibliographic import isbn13
    for asset in state['assets'].values():
        embedded = asset.get('embedded', {})
        code = isbn13(str(embedded.get('ISBN') or embedded.get('GTIN') or ''))
        if code:
            claims.setdefault('isbn', []).append(code)
    claims = {field: sorted(set(values)) for field, values in claims.items()}
    records = {kind: state[kind] for kind in TABLES}
    # These sets include absent-key membership. A new acquisition/destination
    # owner must invalidate the old prepared result under the same guard row.
    scopes = store._guard_keys(records, claims)
    memberships: dict[str, Any] = {}
    for key in sorted(scopes):
        if key.startswith('acquisition:'):
            value = key.removeprefix('acquisition:')
            memberships[key] = [list(row) for row in db.execute(
                'SELECT id,payload FROM origins WHERE source_ref=%s ORDER BY id',(value,))]
        elif key.startswith('native-file:'):
            _, pid, fid = key.split(':')
            # Equality (or IS NULL) has the meaning of IS NOT DISTINCT FROM and can use the pid and fid indexes.
            lookup = [(column, int(raw) if raw != 'None' else None) for column, raw in (('pid', pid), ('fid', fid))]
            memberships[key] = [list(row) for row in db.execute(
                'SELECT id,payload FROM origins WHERE ' + ' AND '.join(c + (' IS NULL' if v is None else '=%s') for c, v in lookup)
                + ' ORDER BY id', tuple(v for _, v in lookup if v is not None))]
        elif key.startswith('destination:'):
            value = key.removeprefix('destination:')
            memberships[key] = []
            for kind in TABLES:
                # PostgreSQL maintains this expression index for all entity writes,
                # including COPY imports. Only matching payloads enter the token.
                rows = db.execute(f'SELECT id,payload FROM {kind} WHERE '
                    'destination_memberships_v1(payload) @> ARRAY[%s]::text[] ORDER BY id',(value,))
                memberships[key].extend([kind,*row] for row in rows)
    token = {'schema': 1, 'authority': store.authority, 'media': False, 'action': action, 'refs': refs, 'claims': claims,
             'covers': covers,
             'entities': {kind: sorted(state[kind]) for kind in TABLES},
             'fingerprint': digest({'records': records, 'payloads':payloads,
                                    'memberships': memberships})}
    return state, token


def verify(store: Store, db: DeadlineConnection, token: dict[str, Any], change: Change | None = None) -> None:
    if token.get('schema') != 1:
        raise LibraryError('Unknown result conflict token')
    state, current = snapshot(store, db, token['action'], token['refs'], token['claims'], media=token['media'])
    if current != token:
        raise LibraryError('Result conflict: relevant inputs changed')
    if change is None:
        return
    for kind in TABLES:
        for ident in set(change.upserts.get(kind, {})) | set(change.deletes.get(kind, [])):
            if ident not in token['entities'][kind]:
                row = db.execute(f'SELECT id FROM {kind} WHERE id=%s', (ident,)).fetchone()
                if row or ident in change.deletes.get(kind, []):
                    raise LibraryError('Result conflict: undeclared existing entity')
