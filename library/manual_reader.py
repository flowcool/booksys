"""Explicit proof requests use the existing OCR queue and resource authority."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from typing import Iterator
import uuid
import time
import json
from typing import Any
from library.primitives import atomic_yaml, digest
from library.source_tasks import bounded_read

from library.catalog import Catalog
from library.decoder_ownership import DecoderOwnership
from library.primitives import LibraryError, lock
from library.source_tasks import SourcePolicy
from library.stage_execution import QueueOwner, acting
from library.stage_model import Obligation, StepResult
from library.stage_resources import Resources, Demand
from library.stage_tasks import StageTasks

METHOD = 'manual-proof-v1'


@contextmanager
def command_grant(catalog: Catalog, stage: str, identity: str) -> Iterator[None]:
    store = catalog.backend()
    if store is None:
        raise LibraryError('Compose proof requires selected PostgreSQL authority')
    directory = catalog.directory
    tasks = StageTasks(store, policy=SourcePolicy(directory/'task-policy.yaml'))
    resources = Resources(directory/'stage-resources.yaml')
    stamp = datetime.now(timezone.utc)
    # Reuse the actual OCR owner lock. The interactive request cannot overlap
    # its automatic actor or replace an unresolved automatic execution.
    with (directory/'stage-checkpoint.lock').open('a') as boundary, QueueOwner(directory, stage) as owner:
        fcntl.flock(boundary, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if tasks.active(stage) is not None:
            raise LibraryError('OCR queue has an unresolved execution')
        from library.external_capacity import external_capacity
        root = directory.parent
        task = tasks.discover(Obligation(stage, 'cli:'+identity, uuid.uuid4().hex, 'manual-command-v1'), now=stamp)
        key = task.key+':'+str(task.generation+1)
        reason = resources.admit(key, age=stamp, demand=Demand(memory_mib=1792, cpu_millis=1250,
            disk=1, output_bytes=(12 if stage in ('intake', 'copies') else 4)*1024**3),
            free_bytes=shutil.disk_usage(os.environ['BOOKSYS_READER_JOBS']).free,
            external_bytes=external_capacity(root/'snapshots/external-capacity.yaml', now=stamp))
        if reason:
            resources.cancel_wait(key)
            # Terminal without execution. A later explicit click is a new request.
            tasks.prestart_failure(task.key, task.generation, now=stamp)
            raise LibraryError('Proof resource admission deferred: '+reason)
        children = DecoderOwnership.create(directory/'stage-children'/(key+'.yaml'))
        task = tasks.start(task.key, task.generation, now=stamp, resources=True)
        try:
            with children.track(), acting(owner, tasks, task):
                yield
        except BaseException:
            tasks.finish(task.key, task.generation, now=datetime.now(timezone.utc), outcome='uncertain')
            raise
        else:
            tasks.finish(task.key, task.generation, now=datetime.now(timezone.utc), outcome='done', proof='explicit-proof-returned')
        finally:
            if children.cleanup():
                resources.release(key, stopped=True, children_stopped=True)


def submit_page(catalog: Catalog, asset: str, page: int, *, timeout: float = 175) -> dict[str, Any]:
    """Submit to the existing OCR owner; never acquire its lifetime lock."""
    if len(asset) != 64 or any(c not in '0123456789abcdef' for c in asset) or type(page) is not int or not 1 <= page <= 2000:
        raise LibraryError('Invalid explicit proof request')
    store = catalog.backend()
    if store is None:
        raise LibraryError('Proof requires selected task authority')
    reference = uuid.uuid4().hex
    value = {'asset': asset, 'page': page}
    directory = catalog.directory/'proof-requests'
    directory.mkdir(mode=0o700, exist_ok=True)
    atomic_yaml(directory/(reference+'.yaml'), value, exclusive=True)
    from library.operational_state import ManualProofs
    proofs=ManualProofs(store._database)
    proofs.retain(reference,value)
    tasks = StageTasks(store)
    task = tasks.discover(Obligation('ocr', 'proof:'+reference, digest(value), METHOD), now=datetime.now(timezone.utc))
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result=proofs.result(reference)
        if result is not None:
            return result
        retained = tasks.get(task.key)
        if retained is None or retained.status in ('blocked', 'failed', 'done'):
            raise LibraryError('Proof execution stopped without a retained result')
        time.sleep(.1)
    raise LibraryError('Proof request retained in OCR queue; result not yet available')


def execute_page(catalog: Catalog, task: Any, resolution: Any) -> StepResult:
    import re
    import yaml
    from library.stage_model import StepResult
    reference = task.entity.removeprefix('proof:')
    if not re.fullmatch('[0-9a-f]{32}', reference):
        raise LibraryError('Invalid retained proof reference')
    directory = catalog.directory/'proof-requests'
    from library.operational_state import ManualProofs
    proofs=ManualProofs(catalog.backend()._database)
    value=proofs.request(reference)
    if set(value) != {'asset', 'page'} or digest(value) != task.input_revision:
        raise LibraryError('Retained proof request changed')
    evidence=directory/(reference+'.result.yaml')
    if evidence.exists():
        result=dict(yaml.safe_load(bounded_read(evidence)))
        if result.get('asset')!=value['asset'] or result.get('page')!=value['page']:
            raise LibraryError('Retained proof result identity changed')
    else:
        result=resolution._page(value['asset'],value['page'])
        atomic_yaml(evidence,result,exclusive=True)
    prepared=catalog.prepare(catalog.working_set('resolution_result',{}))
    prepared.completions.append(proofs.prepare(reference,result))
    return StepResult('done',proof='proof-result:'+reference,prepared=prepared)


def page_receipt(catalog: Catalog, task: Any) -> tuple[str,str] | StepResult:
    from library.operational_state import ManualProofs
    result=ManualProofs(catalog.backend()._database).result(task.entity.removeprefix('proof:'))
    if result is not None:return 'done','retained-proof-result'
    evidence=catalog.directory/'proof-requests'/(task.entity.removeprefix('proof:')+'.result.yaml')
    if evidence.is_file():return execute_page(catalog,task,None)
    return 'absent','manual-proof-evidence-rescan'
