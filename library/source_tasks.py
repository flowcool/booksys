"""PostgreSQL source policy and bounded, non-executing source discovery."""
from __future__ import annotations

from dataclasses import dataclass
import json
import copy
import hashlib
import os
import stat
from pathlib import Path
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from library.postgres import DeadlineConnection
    from library.store import Store, SourceProgress

import yaml

from library.primitives import LibraryError, digest
from library.stage_model import Task

MAX_BYTES = 4194304
MAX_POLICY_BYTES = 8388608
MAX_SOURCES = 16
PAGE = 256


def bounded_read(path: Path, *, max_bytes: int = MAX_BYTES) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise LibraryError('source_journal_not_regular')
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise LibraryError('source_journal_capacity')
    return raw


from library.stage_model import Scan as Scan


class SourcePolicy:
    def __init__(self, path: Path, *, store: Store | None = None):
        self.path = path
        if store is None:
            from library.catalog import Catalog
            store = Catalog(path.parent).backend()
        self.store = store
        self._journal_memo: dict[tuple[str, str | None], tuple[tuple[tuple[int, int, int, int], ...], dict[str, Any]]] = {}

    @classmethod
    def create(cls, path: Path, *, store: Store | None = None) -> SourcePolicy:
        # The schema is initialized by the explicit catalogue initializer.
        return cls(path, store=store)

    def read(self) -> dict[str, Any]:
        from library.codec import decode
        with self.store._database.connection() as db:
            self.store.verify(db)
            sources={key:decode(payload) for key,payload in db.execute('SELECT id,payload FROM source_registry')}
            for value in sources.values():
                value['inputs']={}
                if value.get('native'):value['native'].update(inputs={},guards={},bindings={})
            for source,entity,revision,guard,binding_raw in db.execute('SELECT source,entity,input_revision,guard,binding FROM source_inputs'):
                value=sources[source];value['inputs'][entity]=revision
                if value.get('native') and binding_raw:
                    binding=decode(binding_raw);local=entity.split(':',1)[1]
                    value['native']['inputs'][local]=revision
                    value['native']['guards'][local]=binding['projection_guard']
                    value['native']['bindings'][local]={k:v for k,v in binding.items() if k not in ('registration_kind','projection_guard')}
            return {'schema':1,'tasks':{},'sources':sources}

    def sources(self) -> dict[str, Any]:
        """The registered journals only (id, queue, path, cursor, last scan): no inputs, no tasks."""
        from library.codec import decode
        with self.store._database.connection() as db:
            self.store.verify(db)
            return {key: decode(payload) for key, payload in db.execute('SELECT id,payload FROM source_registry')}

    def validation_snapshot(self,tasks: list[Task], *, db: DeadlineConnection | None=None) -> tuple[str,set[str]]:
        """Prepare compact native validation outside a queue mutation transaction."""
        if db is None:
            with self.store._database.connection() as snapshot:
                return self._validation(tasks=tasks,db=snapshot)
        return self._validation(tasks=tasks,db=db)

    @staticmethod
    def _validation(*,tasks: list[Task],db: DeadlineConnection) -> tuple[str,set[str]]:
        from library.operational_state import native_validation
        return native_validation(tasks=tasks,db=db)

    def register(self, queue: str, journal: Path, *, kind: str = 'monitor') -> tuple[str, str]:
        from library.codec import encode, decode
        from library.postgres import guards
        if kind != 'monitor':
            raise LibraryError('Invalid native source kind')
        ident = digest({'queue': queue, 'path': str(journal.absolute())})
        with self.store._database.connection(write=True) as db:
            guards(db, {'source-registration'})
            row = db.execute('SELECT payload FROM source_registry WHERE id=%s', (ident,)).fetchone()
            if row:
                return ident, decode(row[0])['cursor']
            count = db.execute('SELECT count(*) FROM source_registry').fetchone()
            if count and count[0] >= MAX_SOURCES:
                raise LibraryError('source_registration_capacity')
            db.execute('INSERT INTO source_registry VALUES (%s,%s)',
                       (ident, encode({'queue': queue, 'path': str(journal.absolute()), 'kind':kind, 'cursor': ''})))
            return ident, ''

    def retain_scan_failure(self, source: str, expected: dict[str, Any], stamp: str) -> None:
        """Rotate a failed scan without changing its cursor or accepted input."""
        from library.codec import decode, encode
        from library.postgres import guards
        with self.store._database.connection(write=True) as db:
            guards(db, {'source:'+source})
            row = db.execute('SELECT payload FROM source_registry WHERE id=%s', (source,)).fetchone()
            if row is None:
                return
            retained = decode(row[0])
            if any(retained.get(key) != expected.get(key) for key in ('cursor', 'last_scan', 'sha256')):
                return
            retained['last_scan'] = stamp
            db.execute('UPDATE source_registry SET payload=%s WHERE id=%s', (encode(retained), source))

    def _snapshot(self, db: DeadlineConnection, source: str, view: dict[str, Any], stamp: str, cursor: str, *, expected: str | None=None) -> None:
        from library.codec import encode, decode
        from library.postgres import guards
        guards(db, {'source:' + source})
        row = db.execute('SELECT payload FROM source_registry WHERE id=%s', (source,)).fetchone()
        if not row:
            raise LibraryError('source_policy_missing')
        if expected is not None and row[0]!=expected:
            raise LibraryError('source_cursor_changed')
        value = decode(row[0])
        value.update(cursor=cursor,sha256=view['_source_sha256'],last_scan=stamp)
        value.pop('inputs',None)
        if view.get('_native'):
            value['native']={'queue':view['_native']['queue']}
        db.execute('UPDATE source_registry SET payload=%s WHERE id=%s', (encode(value),source))
        members=[]
        for entity,record in view['items'].items():
            key=source+':'+entity
            native=view.get('_native',{})
            guard_value=native.get('guards',{}).get(entity,{})
            binding=native.get('bindings',{}).get(entity)
            if binding is not None:
                binding={**binding,'registration_kind':value.get('kind','monitor'),'projection_guard':guard_value}
            members.append({'entity':key,'input_revision':record['input_revision'],'method':record['method'],
                'guard':digest(guard_value),'binding':encode(binding) if binding is not None else None})
        db.execute('INSERT INTO source_inputs(source,entity,input_revision,method,guard,progress,binding) '
                   'SELECT %s,entity,input_revision,method,guard,NULL,binding FROM jsonb_to_recordset(%s::jsonb) '
                   'AS i(entity text,input_revision text,method text,guard text,binding text) ORDER BY entity COLLATE "C" '
                   'ON CONFLICT(source,entity) DO UPDATE SET progress=CASE WHEN source_inputs.input_revision=excluded.input_revision '
                   'AND source_inputs.method=excluded.method AND source_inputs.guard=excluded.guard THEN source_inputs.progress ELSE NULL END, '
                   'input_revision=excluded.input_revision,method=excluded.method,guard=excluded.guard,binding=excluded.binding '
                   'WHERE (source_inputs.input_revision,source_inputs.method,source_inputs.guard,source_inputs.binding) '
                   'IS DISTINCT FROM (excluded.input_revision,excluded.method,excluded.guard,excluded.binding)',
                   (source,json.dumps(members,ensure_ascii=False)))
        db.execute('DELETE FROM source_inputs WHERE source=%s AND NOT entity=ANY(%s::text[])',
                   (source,[row['entity'] for row in members]))

    def prepare_progress(self, task: Task, result: dict[str, Any], *, completions: list[Any] | None = None,
                         reconciled: bool = False) -> SourceProgress:
        from library.store import SourceProgress
        from library.stage_tasks import StageTasks
        with self.store._database.connection() as db:
            if reconciled and task.superseded_by:
                from library.operational_state import verify_superseded_native_readback
                verify_superseded_native_readback(db, task, result['proof'], completions or [])
            else:
                StageTasks._verify_source(db,task)
        return SourceProgress(task.source,task.entity,task.input_revision,task.method,task.source_guard,
                              copy.deepcopy(result),copy.deepcopy(completions or []))

    def progress(self, source: str, entity: str) -> dict[str, Any] | None:
        from library.codec import decode
        with self.store._database.connection() as db:
            row = db.execute('SELECT progress FROM source_inputs WHERE source=%s AND entity=%s',
                             (source,entity)).fetchone()
            return decode(row[0]) if row and row[0] is not None else None

    def source_reason(self, task: Task) -> str:
        try:
            self.verify_task(task)
        except LibraryError as exc:
            return str(exc)
        return ''

    @staticmethod
    def fingerprint(paths: tuple[Path, ...]) -> tuple[tuple[int, int, int, int], ...]:
        result = []
        for path in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                result.append((0, 0, 0, 0))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise LibraryError('source_journal_not_regular')
            result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        return tuple(result)

    def verify_task(self, task: Task) -> None:
        from library.stage_tasks import StageTasks
        with self.store._database.connection() as db:
            StageTasks._verify_source(db,task)

    def journal(self, path: Path, *, queue: str | None = None) -> dict[str, Any]:
        """Project registered canonical rows; the path is bootstrap provenance only."""
        from library.codec import decode
        from library.operational_state import CollectorState
        ident=digest({'queue':queue,'path':str(path.absolute())})
        with self.store._database.connection() as db:
            row=db.execute('SELECT payload FROM source_registry WHERE id=%s',(ident,)).fetchone()
            if row is None:
                raise LibraryError('source_policy_missing')
            source=decode(row[0])
        if queue is None:
            raise LibraryError('Native source queue required')
        value=CollectorState(path.parent,database=self.store._database).native_view(queue,source.get('kind','monitor'))
        # A running operation owns its original accepted input. Its own bounded
        # checkpoints are continuation, not a new business generation.
        from library.stage_tasks import StageTasks
        with self.store._database.connection() as db:
            active=db.execute("SELECT item,input_rev,method,source,attempts,claimed_by,ready_at FROM work_items "
                              "WHERE stage=%s AND state='claimed' AND source=%s LIMIT 1",(queue,ident)).fetchone()
            if active:
                from library.work_actor import task_view
                from library.work_items import Item
                guard_row=db.execute('SELECT guard FROM source_inputs WHERE source=%s AND entity=%s',(ident,active[0])).fetchone()
                task=task_view(Item(queue,*active),source_guard=guard_row[0] if guard_row else '');local=task.entity.split(':',1)[1]
                binding=StageTasks._source_binding(db,task)
                try:StageTasks._verify_source(db,task)
                except LibraryError:pass  # A real changed input keeps its new projection and fences the owner.
                else:
                    if binding is not None and binding['kind']=='category' and local in value['items']:
                        value['items'][local]['input_revision']=task.input_revision
                        value['_native']['inputs'][local]=task.input_revision
                        value['_native']['bindings'][local]={k:v for k,v in binding.items() if k not in ('registration_kind','projection_guard')}
        value['_source_sha256']=digest(value)
        value['_source_registration']=row[0]
        return value



def parse_journal(path: Path, queue: str | None = None) -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    raw = bounded_read(path)
    value = yaml.safe_load(raw)
    if isinstance(value, dict) and 'submissions' in value and queue is not None:
        from library.native_projection import project
        auxiliary_path = path.parent/'fiches.yaml'
        auxiliary = yaml.safe_load(bounded_read(auxiliary_path)) if queue == 'collection' and auxiliary_path.exists() else None
        value = project(queue, value, auxiliary=auxiliary)
    if not isinstance(value, dict) or not isinstance(value.get('items'), dict):
        raise LibraryError('Invalid discovery journal view')
    from library.stage_model import Obligation, instant
    from datetime import datetime
    import re
    for entity, record in value['items'].items():
        if not isinstance(record, dict) or set(record) - {'input_revision', 'method', 'due_at', 'blocked'}:
            raise LibraryError('Invalid source task view')
        Obligation('collection', entity, record['input_revision'], record['method']).key
        if record.get('due_at'):
            instant(datetime.fromisoformat(record['due_at']))
        if record.get('blocked') and not re.fullmatch('[a-z_]{1,80}', record['blocked']):
            raise LibraryError('Invalid source block reason')
    value['_source_sha256'] = hashlib.sha256(raw).hexdigest()
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    import sys
    parse_journal(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
