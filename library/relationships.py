"""Rebuildable, filtered canonical relationships. Refresh belongs to the observer."""
from __future__ import annotations

from collections import defaultdict
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from booksys_core.diagnostics import REF_KINDS, opaque, safe_text
from library.primitives import digest, LibraryError
from library.store import Store, RELATIONS

Node = tuple[str, str]
Edge = tuple[Node, Node, str]
SINGULAR = {'assets': 'asset', 'origins': 'origin'}


class Relationships:
    def __init__(self, store: Store):
        self.store = store
        self.key = secrets.token_bytes(32)
        self.snapshot: dict[str, Any] | None = None

    def refresh(self, *, timestamp: float | None = None, submissions: dict[str, Any] | None = None,
                routing: dict[str, Any] | None = None, retirement: dict[str, Any] | None = None) -> None:
        nodes: dict[Node, dict[str, str]] = {}
        edges: set[Edge] = set()
        def node(kind: str, ident: Any, **milestones: Any) -> Node:
            value = str(ident)
            if kind not in REF_KINDS or not opaque(value):
                raise LibraryError('Invalid canonical relationship identity')
            key = kind, value
            nodes.setdefault(key, {}).update({name: safe_text(str(value)) for name, value in milestones.items()
                                             if value is not None})
            if len(nodes) > 100000:
                raise LibraryError('Relationship observation exceeds node bound')
            return key
        def edge(first: Node, second: Node, role: str) -> None:
            nodes.setdefault(first, {}); nodes.setdefault(second, {})
            edges.add((first, second, role))
            if len(edges) > 400000 or len(nodes) > 100000:
                raise LibraryError('Relationship observation exceeds edge bound')
        def package(pid: Any, **milestones: Any) -> Node:
            if isinstance(pid, bool) or not str(pid).isdigit():
                raise LibraryError('Invalid scoped downloader package')
            return node('package', 'booksys-pyload:' + str(pid), **milestones)
        with self.store._database.connection() as db:
            revision, applied = self.store.check(db)
            for kind, singular in SINGULAR.items():
                for ident, state, identity, disposition, admission in db.execute(
                        f'SELECT id,state,identity_status,disposition,admission_status FROM {kind} ORDER BY id'):
                    node(singular, ident, state=state, identity=identity if kind == 'assets' else None,
                         disposition=disposition, admission=admission)
            for ident, source, pid, fid, asset in db.execute('SELECT id,source_ref,pid,fid,asset_id FROM origins'):
                origin = node('origin', ident)
                if source:
                    edge(node('source', digest(source)), origin, 'source_origin')
                if pid is not None:
                    edge(package(pid), origin, 'package_origin')
                    if source:
                        edge(node('source', digest(source)), package(pid), 'source_package')
                if fid is not None:
                    file = node('file', 'booksys-pyload:' + str(fid))
                    edge(file, origin, 'file_origin')
                    if pid is not None:
                        edge(package(pid), file, 'package_file')
                if asset and ('asset', asset) in nodes:
                    edge(origin, node('asset', asset), 'origin_asset')
            for relation, (first_kind, second_kind) in RELATIONS.items():
                if first_kind in SINGULAR and second_kind in SINGULAR:
                    for first, second in db.execute(f'SELECT source,target FROM {relation}'):
                        edge(node(SINGULAR[first_kind], first), node(SINGULAR[second_kind], second), relation)
        for source, record in (submissions or {}).get('submissions', {}).items():
            if not isinstance(source, str) or not isinstance(record, dict):
                raise LibraryError('Invalid collection observation')
            if record.get('package_id') is not None:
                edge(node('source', digest(source)), package(record['package_id'], state=record.get('status')), 'source_package')
        for record in (routing or {}).get('packages', {}).values():
            if not isinstance(record, dict):
                raise LibraryError('Invalid routing observation')
            source = record.get('source')
            if not isinstance(source, str):
                continue
            source_node = node('source', digest(source))
            primary = record.get('pid')
            if primary is not None:
                edge(source_node, package(primary, phase=record.get('phase')), 'source_package')
            if record.get('attempt_pid') is not None:
                fallback = package(record['attempt_pid'], phase=record.get('phase'))
                edge(source_node, fallback, 'source_package')
                if primary is not None:
                    edge(package(primary), fallback, 'fallback_package')
        for source, record in (retirement or {}).get('acquisitions', {}).items():
            if not isinstance(source, str) or not isinstance(record, dict):
                raise LibraryError('Invalid retirement observation')
            owner = node('source', digest(source), retirement=record.get('phase'))
            for saved in record.get('packages', {}).values():
                retired = package(saved['pid'], retirement=record.get('phase'))
                edge(owner, retired, 'source_package')
                for item in saved.get('links', []):
                    edge(retired, node('file', 'booksys-pyload:' + str(item[0])), 'package_file')
        adjacency: dict[Node, set[Node]] = defaultdict(set)
        for first, second, _ in edges:
            adjacency[first].add(second); adjacency[second].add(first)
        signature = digest({'revision': revision, 'nodes': sorted((list(key), value) for key, value in nodes.items()),
                            'edges': sorted(edges)})
        self.snapshot = {'revision': revision, 'observed_at': time.time() if timestamp is None else timestamp,
                         'generation': signature, 'nodes': nodes, 'edges': sorted(edges), 'adjacency': adjacency}

    def resolve(self, typed_ref: dict[str, str], *, cursor: str | None = None, limit: int = 50,
                timestamp: float | None = None) -> dict[str, Any]:
        if (not isinstance(typed_ref, dict) or set(typed_ref) != {'kind', 'id'}
                or typed_ref['kind'] not in REF_KINDS or not opaque(typed_ref['id'])
                or type(limit) is not int or not 1 <= limit <= 50):
            raise ValueError('Invalid bounded relationship query')
        snapshot = self.snapshot
        if snapshot is None:
            return {'state': 'unavailable', 'nodes': [], 'edges': [], 'cursor': None}
        root = typed_ref['kind'], typed_ref['id']
        start = edge_start = 0
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 8192:
                raise ValueError('Invalid relationship cursor')
            raw = base64.b64decode(cursor, altchars=b'-_', validate=True)
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.digest(self.key, payload, 'sha256'), signature):
                raise ValueError('Invalid relationship signature')
            saved = json.loads(payload)
            if (set(saved) != {'root', 'generation', 'node', 'edge'} or saved['root'] != list(root)
                    or type(saved['node']) is not int or type(saved['edge']) is not int
                    or saved['node'] < 0 or saved['edge'] < 0):
                raise ValueError('Invalid relationship continuation')
            if saved['generation'] != snapshot['generation']:
                return {'state': 'partial', 'nodes': [], 'edges': [], 'cursor': None, 'reason': 'observation_changed'}
            start, edge_start = saved['node'], saved['edge']
        found: set[Node] = set()
        pending = [root] if root in snapshot['nodes'] else []
        while pending:
            current = pending.pop()
            if current not in found:
                found.add(current)
                pending.extend(snapshot['adjacency'].get(current, set()) - found)
        ordered = sorted(found)
        related = [edge for edge in snapshot['edges'] if edge[0] in found and edge[1] in found]
        selected, selected_edges = ordered[start:start + limit], related[edge_start:edge_start + 200]
        end, edge_end = start + len(selected), edge_start + len(selected_edges)
        continuation = None
        if end < len(ordered) or edge_end < len(related):
            payload = json.dumps({'root': list(root), 'generation': snapshot['generation'], 'node': end,
                                  'edge': edge_end}, sort_keys=True).encode()
            continuation = base64.urlsafe_b64encode(payload + hmac.digest(self.key, payload, 'sha256')).decode()
        stamp = time.time() if timestamp is None else timestamp
        return {'state': 'continued' if continuation else 'complete', 'cursor': continuation,
                'observed_at': snapshot['observed_at'], 'age_seconds': max(0, stamp - snapshot['observed_at']),
                'catalogue_revision': snapshot['revision'],
                'nodes': [{'kind': kind, 'id': ident, 'milestones': snapshot['nodes'][(kind, ident)]} for kind, ident in selected],
                'edges': [{'from': {'kind': first[0], 'id': first[1]}, 'to': {'kind': second[0], 'id': second[1]},
                           'relation': role} for first, second, role in selected_edges]}
