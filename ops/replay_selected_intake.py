"""Run exact cached-recovery sources through the normal owned intake actor."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import threading
import time
from typing import Any,Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.authority import read_manifest
from library.operational_state import CollectorState,RuntimeObservation
from library.metadata import Providers
from library.primitives import digest
from library.stage_model import Obligation, METHODS
from library.stage_runtime import StageRuntime
from library.stage_worker import snapshot
from library.stage_inputs import origin_refs
from library.readers import fingerprint, sha256
from library.reader_client import available
from library.store import PreparedResult
from booksys_core.downloader import Pyload


def filtered_receiver(receive: Callable[...,dict[str,Any]], pairs: set[tuple[int,int]]) -> Callable[...,dict[str,Any]]:
    def receive_selected(store: Any, root: Path, captured: dict[str, Any], journal: dict[str, Any],
                         queue: list[dict[str, Any]], *, stable_seconds: float = 2,
                         incremental: bool = False, inspection_limit: int | None = None,
                         source_only: str | None = None, prepared: list[PreparedResult] | None=None,
                         media_directory: Path | None=None) -> dict[str, Any]:
        narrowed = copy.deepcopy(queue)
        for package in narrowed:
            package['links'] = [item for item in package.get('links', [])
                                if (package['pid'], item['fid']) in pairs]
        return receive(store, root, captured, journal, narrowed, stable_seconds=stable_seconds,
                       incremental=incremental, inspection_limit=inspection_limit,source_only=source_only,prepared=prepared,media_directory=media_directory)
    return receive_selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    root = Path('/data/booksys/library-lab')
    read_manifest(root/'state')
    selected = json.loads(args.selection.read_text())['origins']
    if not 1 <= len(selected) <= 26 or len({row['id'] for row in selected}) != len(selected):
        raise SystemExit('Select one to26 distinct retained origins')
    client = Pyload('http://pyload:8000', json.loads(Path('/secrets/pyload.json').read_text())['api_key'])
    runtime = StageRuntime(root, Path('/data/booksys/downloads'), Path('/data/booksys/state'),
        providers=Providers(root/'state/library-provider-cache', budget=0), client=client, browser=None)
    backend = runtime.catalog.backend()
    if backend is None:
        raise SystemExit('Selected PostgreSQL authority is required')
    # Match the existing format-retry command: narrow only the immutable queue
    # passed to intake. Keep the shared observation and all catalogue origins.
    import library.intake
    receive = library.intake.receive
    pairs = set()
    for row in selected:
        origin = backend.get('origins', row['id'], media=False)
        if origin is None:
            raise SystemExit('Selected origin is missing')
        pairs.add((origin['pid'], origin['fid']))
    library.intake.receive = filtered_receiver(receive,pairs)
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    with runtime.actor('intake') as actor:
        # Keep the launcher's asynchronous NAS readiness observation fresh while
        # the client hashes large sources. This performs only the probe RPC.
        stop_probe = threading.Event()
        def probe() -> None:
            while not stop_probe.is_set():
                if runtime.nas is not None:
                    runtime.nas.ready()
                available()
                stop_probe.wait(1)
        pulse = threading.Thread(target=probe, daemon=True)
        pulse.start()
        # Exact filtering changes only this one-shot dispatch. The normal actor
        # still checks eligibility, attempts, readiness, exclusion and recovery.
        originals = {}
        for row in selected:
            origin = backend.get('origins', row['id'], media=False)
            if origin is None or origin['path'] != row['path']:
                raise SystemExit('Selected origin drift')
            path = runtime.reception/origin['path']
            if list(fingerprint(path)) != row['fingerprint'] or sha256(path) != row['sha256']:
                raise SystemExit('Selected source changed')
            originals[row['id']] = origin
        sources = {row['source'] for row in originals.values()}
        entities = {'source-'+digest(source) for source in sources}
        with runtime.tasks.store._database.connection() as db:
            retained = [json.loads(payload) for (payload,) in db.execute(
                "SELECT payload FROM stage_tasks WHERE stage='intake'")]
        by_key = {task['key']: task for task in retained}
        for task in retained:
            if (task['status'] == 'active' or task['blocked'] == 'effect_uncertain') and task['entity'] not in entities:
                raise SystemExit('Unselected intake recovery must finish first')
        for reference in (*runtime.resources.grants(), *runtime.resources.waiters()):
            task = by_key.get(reference.rsplit(':', 1)[0])
            if task is not None and task['entity'] not in entities:
                raise SystemExit('Unselected intake ownership must finish first')
        original_select = runtime.tasks.select
        def select(queue: str, **kwargs: Any) -> Any:
            with runtime.tasks.store._database.connection() as db:
                excluded = {key: 'outside_selected_recovery' for key, payload in
                    db.execute("SELECT key,payload FROM stage_tasks WHERE stage='intake'")
                    if json.loads(payload)['entity'] not in entities}
            kwargs['resources'] = {**excluded, **kwargs.get('resources', {})}
            return original_select(queue, **kwargs)
        runtime.tasks.select = select  # type: ignore[method-assign]
        outcomes = []
        snapshot_at = 0.0
        for cycle in range(300):
            if time.monotonic() >= snapshot_at:
                snapshot(runtime)  # Native GET only; refresh exact queue observations.
                snapshot_at = time.monotonic()+60
            database=runtime.catalog.backend()._database
            monitor=CollectorState(runtime.collector,database=database).monitor()
            packages=RuntimeObservation(database).queue()[0]
            for source in sources:
                entry = monitor['submissions'][source]
                package = next(p for p in packages if p['pid'] == entry['package_id'])
                if package.get('retirement'):
                    raise SystemExit('Retired package cannot use filtered intake replay')
                if entry['status'] != 'queued' or not (package.get('retirement') or
                        any(item.get('status') == 0 for item in package.get('links', []))):
                    raise SystemExit('Selected source is not eligible for normal discovery')
                state = runtime.catalog.working_set('retire', origin_refs(runtime.catalog, source), media=False)
                revision = digest({'package': {key: package.get(key) for key in ('pid', 'name', 'folder', 'retirement')},
                    'files': [{key: item.get(key) for key in ('fid', 'name', 'status', 'size', 'input_folder')} for item in package.get('links', [])],
                    'origins': {oid: {key: row.get(key) for key in ('sha256', 'fingerprint', 'availability', 'intake_attempts', 'bundle_attempts')}
                                for oid, row in state['origins'].items()}})
                runtime.tasks.discover(Obligation('intake', 'source-'+digest(source), revision, METHODS['intake']),
                                       now=datetime.now(timezone.utc))
            result = actor.step(now=datetime.now(timezone.utc), free_bytes=shutil.disk_usage(root/'library').free,
                                external_bytes=None)
            outcomes.append(result)
            current = {oid: backend.get('origins', oid, media=False) for oid in originals}
            proof = {'cycles': cycle+1, 'outcomes': outcomes, 'origins': current}
            (args.evidence/'result.json').write_text(json.dumps(proof, indent=2))
            if all(row and row.get('fingerprint') for row in current.values()):
                print('All selected origins re-inspected')
                return
            if result in ('uncertain', 'cleanup_required', 'reconciliation_required'):
                raise SystemExit('Retained effect requires reconciliation; no automatic replay')
            time.sleep(1)
    raise SystemExit('Selected work remains deferred; inspect exact retained result')


if __name__ == '__main__':
    main()
