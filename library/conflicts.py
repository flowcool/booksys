"""Relevant catalogue closures, membership and immutable-decision claims."""
from __future__ import annotations

from library.postgres import DeadlineConnection
from typing import Any, TYPE_CHECKING

from library.codec import decode
from library.primitives import TABLES, LibraryError, digest
if TYPE_CHECKING:
    from library.store import Store, Change


def snapshot(store: Store, db: DeadlineConnection, action: str, refs: dict[str, list[str]],
             claims: dict[str, list[str]], *, media: bool = True, ignore_decision: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
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
    for record in state['editions'].values():
        for field in ('isbn', 'reference'):
            if record.get(field):
                claims.setdefault(field, []).append(str(record[field]))
    from library.bibliographic import isbn13
    for asset in state['assets'].values():
        embedded = asset.get('embedded', {})
        code = isbn13(str(embedded.get('ISBN') or embedded.get('GTIN') or ''))
        if code:
            claims.setdefault('isbn', []).append(code)
    claims = {field: sorted(set(values)) for field, values in claims.items()}
    records = {kind: state[kind] for kind in TABLES}
    decisions: dict[str, Any] = {}
    for kind, ids in (('assets', set(state['assets']) | set(refs.get('assets', []))),
                      ('editions', set(state['editions']) | set(refs.get('editions', [])))):
        clause = 'target=%s' if kind == 'assets' else 'id IN (SELECT decision FROM decision_editions WHERE edition=%s)'
        for ident in ids:
            for key, payload in db.execute('SELECT id,payload FROM ledger_records WHERE ' + clause, (ident,)):
                decisions[key] = decode(payload)
            reservation_clause = 'target=%s' if kind == 'assets' else "scope::jsonb ? ('decisions:editions:' || %s)"
            for key, payload in db.execute("SELECT id,payload FROM decision_reservations WHERE status='reserved' AND "
                                           + reservation_clause,(ident,)):
                decisions[key] = decode(payload)
    if ignore_decision is not None:
        decisions.pop(ignore_decision,None)
    owners: dict[str, Any] = {}
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
            memberships[key] = [list(row) for row in db.execute(
                'SELECT id,payload FROM origins WHERE pid IS NOT DISTINCT FROM %s '
                'AND fid IS NOT DISTINCT FROM %s ORDER BY id',
                (int(pid) if pid != 'None' else None,int(fid) if fid != 'None' else None))]
        elif key.startswith('destination:'):
            value = key.removeprefix('destination:')
            memberships[key] = []
            for kind in TABLES:
                # PostgreSQL maintains this expression index for all entity writes,
                # including COPY imports. Only matching payloads enter the token.
                rows = db.execute(f'SELECT id,payload FROM {kind} WHERE '
                    'destination_memberships_v1(payload) @> ARRAY[%s]::text[] ORDER BY id',(value,))
                memberships[key].extend([kind,*row] for row in rows)
    for field, values in sorted(claims.items()):
        if field not in ('isbn', 'reference'):
            raise LibraryError('Unknown ownership claim')
        for value in sorted(set(values)):
            owners[field + ':' + value] = {
                'editions': db.execute('SELECT edition FROM edition_claims WHERE field=%s AND value=%s ORDER BY edition', (field, value)).fetchall(),
                'decisions': db.execute('SELECT decision FROM decision_claims WHERE field=%s AND value=%s ORDER BY decision', (field, value)).fetchall(),
                'reservations': db.execute("SELECT id FROM decision_reservations WHERE status='reserved' AND "
                    "scope::jsonb ? %s ORDER BY id",('claim:'+field+':'+value,)).fetchall()}
    token = {'schema': 1, 'authority': store.authority, 'media': False, 'action': action, 'refs': refs, 'claims': claims,
             'covers': covers,
             'entities': {kind: sorted(state[kind]) for kind in TABLES},
             'fingerprint': digest({'records': records, 'payloads':payloads, 'decisions': decisions, 'claims': owners,
                                    'memberships': memberships})}
    return state, token


def verify(store: Store, db: DeadlineConnection, token: dict[str, Any], change: Change | None = None, *, projection: str | None = None, completion_attempt: tuple[str,int] | None=None) -> None:
    if token.get('schema') != 1:
        raise LibraryError('Unknown result conflict token')
    state, current = snapshot(store, db, token['action'], token['refs'], token['claims'], media=token['media'])
    if current!=token and projection is not None and completion_attempt is not None:
        if store._completion_projection(db,projection,completion_attempt,token):
            state,current=snapshot(store,db,token['action'],token['refs'],token['claims'],media=token['media'],ignore_decision=projection)
    if current != token:
        raise LibraryError('Result conflict: relevant inputs changed')
    affected = {kind:dict(state[kind]) for kind in TABLES}
    for kind, ids in token['refs'].items():
        for ident in ids:
            affected[kind].setdefault(ident,{})
    if change is not None:
        for kind, records in change.upserts.items():
            affected[kind].update(records)
    store._check_decision_reservations(db,store._guard_keys(affected,token['claims']),projection=projection)
    if change is None:
        return
    # The concrete decision projection additionally holds the chain guard and
    # checks the immutable prefix inside Store.commit.
    for kind in TABLES:
        for ident in set(change.upserts.get(kind, {})) | set(change.deletes.get(kind, [])):
            if ident not in token['entities'][kind]:
                row = db.execute(f'SELECT id FROM {kind} WHERE id=%s', (ident,)).fetchone()
                if row or ident in change.deletes.get(kind, []):
                    raise LibraryError('Result conflict: undeclared existing entity')
        if kind == 'editions':
            for record in change.upserts.get(kind, {}).values():
                for field in ('isbn', 'reference'):
                    value = record.get(field)
                    if not value:
                        continue
                    owners = [r[0] for r in db.execute('SELECT edition FROM edition_claims WHERE field=%s AND value=%s', (field, str(value)))]
                    pending = db.execute('SELECT decision FROM decision_claims WHERE field=%s AND value=%s', (field, str(value))).fetchall()
                    if (any(owner not in token['entities']['editions'] for owner in owners) or pending) and str(value) not in token['claims'].get(field, []):
                        raise LibraryError('Result conflict: undeclared competing claim')
