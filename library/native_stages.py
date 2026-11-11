"""One retained collection, routing or retirement step; native downloads stay in pyLoad."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from library.catalog import Catalog
from library.primitives import LibraryError, digest, lock
from library.source_tasks import bounded_read
from library.stage_model import Task, StepResult, Obligation, METHODS
from library.stage_tasks import StageTasks
from library.stage_execution import effect_scopes, record_scopes


from library.stage_inputs import origin_refs
from library.operational_state import CollectorState,RuntimeObservation,OperationalState,OperationalCompletion,NativeHistory


class NativeStages:
    def __init__(self, catalog: Catalog, reception: Path, library: Path, collector: Path,
                 *, browser: Any, client: Any, snapshots: Path | None = None):
        self.catalog, self.reception, self.library, self.collector = catalog, reception, library, collector
        self.browser, self.client = browser, client
        self.snapshots = snapshots

    def discover_intake(self, tasks: StageTasks, *, now: datetime) -> int:
        if self.snapshots is None:
            raise LibraryError('Intake requires validated queue snapshots')
        monitor = CollectorState(self.collector,database=self.catalog.backend()._database).monitor()
        queue,_ = RuntimeObservation(self.catalog.backend()._database).queue()
        count = 0
        cursor_state=OperationalState(self.catalog.backend()._database)
        cursor={'after':cursor_state.discovery('intake')}
        sources = sorted(source for source in monitor['submissions'] if source > cursor['after'])[:100]
        for source in sources:
            entry = monitor['submissions'][source]
            package = next((p for p in queue if p['pid'] == entry.get('package_id')), None)
            if entry.get('status') != 'queued' or package is None:
                continue
            if not package.get('retirement') and not any(item.get('status') == 0 for item in package.get('links', [])):
                continue
            state = self.catalog.working_set('retire', origin_refs(self.catalog, source), media=False)
            revision = digest({'package': {key: package.get(key) for key in ('pid', 'name', 'folder', 'retirement')},
                'files': [{key: item.get(key) for key in ('fid', 'name', 'status', 'size', 'input_folder')} for item in package.get('links', [])],
                'origins': {oid: {key: row.get(key) for key in ('sha256', 'fingerprint', 'availability', 'intake_attempts', 'bundle_attempts')}
                            for oid, row in state['origins'].items()}})
            tasks.discover(Obligation('intake', 'source-' + digest(source), revision, METHODS['intake']), now=now)
            count += 1
        cursor_state.save_discovery(sources[-1] if len(sources) == 100 else '',kind='intake')
        return count

    def binding(self, task: Task) -> tuple[str, dict[str, Any], dict[str, Any]]:
        monitor = CollectorState(self.collector,database=self.catalog.backend()._database).monitor()
        entity = task.entity.split(':')[-1]
        entries = monitor['categories'] if entity.startswith('category-') else monitor['submissions']
        prefix = 'category-' if entity.startswith('category-') else 'fiche-' if entity.startswith('fiche-') else 'source-'
        sources = [source for source in entries if prefix + digest(source) == entity]
        if len(sources) != 1:
            raise LibraryError('Native source binding changed')
        source = sources[0]
        return source, dict(entries[source]), monitor

    def scopes(self, task: Task) -> list[str]:
        if task.stage == 'collection':
            return []
        source, _, _ = self.binding(task)
        state = self.catalog.working_set('retire', origin_refs(self.catalog, source), media=False)
        return [*record_scopes(state), 'acquisition:' + digest(source)]

    def ready(self, task: Task, *, now: datetime) -> str:
        if task.stage == 'collection':
            return ''
        if self.snapshots is None:
            return 'queue_snapshot_unavailable'
        try:
            queue,observed_at=RuntimeObservation(self.catalog.backend()._database).queue()
            if not 0 <= now.timestamp() - observed_at <= 90:
                return 'queue_snapshot_stale'
            source, entry, monitor = self.binding(task)
            package = next((p for p in queue if p['pid'] == entry.get('package_id')), None)
            if package is None:
                return 'native_package_unavailable'
            state = self.catalog.working_set('retire', origin_refs(self.catalog, source), media=False)
            if task.stage == 'routing':
                from library.scheduling import routing_candidates
                journal = self.collector/'mirror-routing.yaml'
                routing = NativeHistory(journal.parent).routing()
                return '' if source in routing_candidates(queue, {source: entry}, state, routing) else 'routing_prerequisites'
            if task.stage == 'retirement':
                origins = state['origins'].values()
                if not origins or any(o.get('availability') not in ('validated', 'bundle', 'multipart_member') for o in origins):
                    return 'retirement_prerequisites'
            return ''
        except (OSError, LibraryError, KeyError, ValueError, TypeError, yaml.YAMLError):
            return 'native_prerequisites_unavailable'

    def execute(self, task: Task, *, now: datetime) -> StepResult:
        completions: list[OperationalCompletion]=[]
        result=self._execute(task,now=now,completions=completions)
        if task.source and result.outcome=='done':
            from library.source_tasks import SourcePolicy
            from dataclasses import replace
            progress=SourcePolicy(self.catalog.directory/'task-policy.yaml',store=self.catalog.backend()).prepare_progress(
                task,{'proof':result.proof,'stage':task.stage,'results':[{'kind':item.kind,'identity':item.identity,'value':item.value} for item in completions]},completions=completions)
            result=replace(result,source_progress=progress)
        return result

    def _execute(self, task: Task, *, now: datetime, completions: list[OperationalCompletion]) -> StepResult:
        value: Any
        if task.stage == 'collection' and task.entity.split(':')[-1] == 'backfill':
            from booksys_core.collection import run_backfill
            value = run_backfill(self.collector, self.browser, self.client, max_items=1, max_pages_per_call=1,completions=completions)
            return StepResult('done', 'backfill:' + digest(value))
        source, entry, monitor = self.binding(task)
        if task.stage == 'intake':
            if self.snapshots is None:
                raise LibraryError('Intake snapshot authority missing')
            from library.intake import receive
            captured = CollectorState(self.collector,database=self.catalog.backend()._database).captured()
            queue,_ = RuntimeObservation(self.catalog.backend()._database).queue()
            from library.store import PreparedResult
            intake_results: list[PreparedResult]=[]
            state = receive(self.catalog, self.reception, captured, monitor, queue,
                    source_only=source, incremental=True, inspection_limit=1,prepared=intake_results)
            from booksys_core.diagnostics import emit, opaque
            import hashlib
            # Reuse the committed scoped intake result. No diagnostic-only I/O.
            for oid, origin in list(state['origins'].items())[:32]:
                refs = [{'kind': 'task', 'id': task.key}, {'kind': 'source', 'id': digest(source)}]
                for kind, value in (('origin', oid), ('package', str(origin.get('pid', ''))),
                                    ('file', str(origin.get('fid', ''))), ('asset', origin.get('sha256', ''))):
                    if opaque(value):
                        refs.append({'kind': kind, 'id': value})
                aid = origin.get('sha256')
                rejected = origin.get('availability') == 'rejected'
                emit(service='library', stage='local', queue='intake',
                     code='failed' if rejected else 'progress',
                     reason='invalid_archive' if rejected else 'none', refs=refs,
                     request_id=hashlib.sha256((aid or task.entity).encode()).hexdigest())
            return StepResult('done',proof='intake:'+digest({'source':source,'origins':state['origins']}),prepared=intake_results[0])
        if task.stage == 'collection':
            from booksys_core.collection import run_once, run_fiche_once
            if task.entity.split(':')[-1].startswith('category-'):
                value = run_once(self.collector, self.browser, self.client, category=source, step_limit=1,completions=completions)
            else:
                import os
                fiche_root = Path(os.environ.get('BOOKSYS_FICHE_ROOT', str(self.reception)))
                value = run_fiche_once(self.collector, self.browser, self.client, fiche_root, source_only=source,completions=completions)
                progress = CollectorState(self.collector,database=self.catalog.backend()._database).fiches()
                if not completions and progress.get('items', {}).get(source, {}).get('status') != 'complete':
                    return StepResult('retry')
        else:
            state = self.catalog.result_set('retire', origin_refs(self.catalog, source), media=False)
            scopes = record_scopes(state)
            scopes.append('acquisition:' + digest(source))
            attempt = (task.key, task.generation) if task.native_attempt is not None else None
            with effect_scopes(self.catalog.directory, scopes):
                if task.stage == 'routing':
                    from mirror_routing import route_package
                    from mirrors import public_hosts
                    captured = CollectorState(self.collector,database=self.catalog.backend()._database).captured()
                    with lock(self.collector/'pipeline.lock', blocking=False):
                        value = route_package(self.client, source, entry, captured['items'][source], state,
                            self.reception, self.collector/'mirror-routing.yaml', apply=True, availability=public_hosts(),completions=completions,task_attempt=attempt)
                elif task.stage == 'retirement':
                    from library.retirement import Retirement
                    from library.runtime import configured_scripts
                    value = Retirement(self.client, self.catalog, self.reception, self.collector/'monitor.yaml',
                        self.collector/'state.yaml', scripts=configured_scripts(), concurrent=True).process(source, apply=True,completions=completions,task_attempt=attempt)
                else:
                    raise LibraryError('Not a native source stage')
        if (task.stage == 'retirement' and isinstance(value, dict)
                and value.get('outcome') == 'waiting' and value.get('reason') == 'processing_lock_busy'):
            # Retirement proved its entry locks were unavailable before native
            # intent. No business attempt or uncertain side effect was spent.
            return StepResult('defer', 'retirement-entry-lock-busy')
        outcome = 'retry' if isinstance(value, dict) and value.get('outcome') in ('blocked', 'pending', 'waiting') else 'done'
        # This receipt binds the adapter's returned readback and retained source
        # identity. Native effect journals remain the canonical recovery proof.
        return StepResult(outcome, 'native:' + digest({'stage': task.stage, 'entity': task.entity, 'result': value}))

    def receipt(self, task: Task) -> tuple[str,str] | StepResult:
        completions: list[OperationalCompletion]=[]
        outcome,proof=self._receipt(task,completions=completions)
        if outcome=='done' and task.source:
            from library.source_tasks import SourcePolicy
            progress=SourcePolicy(self.catalog.directory/'task-policy.yaml',store=self.catalog.backend()).prepare_progress(
                task,{'proof':proof,'results':[{'kind':item.kind,'identity':item.identity,'value':item.value} for item in completions]},completions=completions,reconciled=True)
            return StepResult(outcome,proof,source_progress=progress)
        return outcome,proof

    def _readback_binding(self, task: Task) -> tuple[str, dict[str, Any]]:
        if task.native_attempt is not None:
            retained = task.native_attempt
            if (retained['task'], retained['generation']) != (task.key, task.generation):
                raise LibraryError('Native readback attempt changed')
            source: str = retained['source']
            entry: dict[str, Any] = retained['entry']
            if digest(entry) != task.input_revision:
                raise LibraryError('Native readback input changed')
            return source, entry
        source, entry, _ = self.binding(task)
        # An imported attempt has no new task-to-operation marker. Its current
        # entry is usable only while it is still the exact original submission.
        if task.source and (task.superseded_by or digest(entry) != task.input_revision):
            raise LibraryError('Historical native readback input changed')
        return source, entry

    def _receipt(self, task: Task, *, completions: list[OperationalCompletion]) -> tuple[str,str]:
        """Resolve existing effects only; selection owns any later new work."""
        from booksys_core.network_admission import NativeMutation
        from booksys_core.errors import CollectionError
        if task.stage == 'intake':
            # Intake only writes idempotent local records and immutable bundle
            # inputs. Its next step revalidates exact source fingerprints.
            return 'absent', 'intake-rescan:' + task.input_revision
        budget = getattr(self.client, 'admission', None)
        if budget is None:
            return 'uncertain', ''
        pointer = NativeMutation(budget.path.parent, 'readback', self.collector/'monitor.yaml').read()
        if pointer['state'] != 'uncertain' and task.stage not in ('retirement','routing'):
            return 'absent', 'native-journal-rescan:' + digest(pointer)
        try:
            if task.stage == 'collection':
                # Recovery never invokes Bookys or creates another package.
                with lock(self.collector/'pipeline.lock', blocking=False):
                    monitor = CollectorState(self.collector,database=self.catalog.backend()._database).monitor()
                    captured = CollectorState(self.collector,database=self.catalog.backend()._database).captured()
                    for source, entry in monitor['submissions'].items():
                        mutation = NativeMutation(budget.path.parent, 'submission:' + source, self.collector/'monitor.yaml')
                        if mutation.owner == pointer.get('owner') and entry.get('status') in ('creating', 'moving'):
                            self.client.submit(source, captured['items'][source], monitor, self.collector/'monitor.yaml')
                            return 'absent', 'submission-readback:' + digest(monitor['submissions'][source])
            elif task.stage == 'retirement':
                source, entry = self._readback_binding(task)
                from retirement_history import read_history
                from library.retirement import Retirement
                from library.runtime import configured_scripts
                journal = self.collector/'package-retirement.yaml'
                mutation = NativeMutation(budget.path.parent, 'retirement:' + source, journal)
                if task.native_attempt is not None and task.native_attempt['journal'] != str(journal.absolute()):
                    return 'uncertain', ''
                record = read_history(journal)['acquisitions'].get(source, {})
                if not record and not task.superseded_by:
                    # A delete is always preceded by a durable domain intent.
                    # Hold both writer lanes while proving that no intent exists
                    # and the original submitted package still has its intake IDs.
                    from mirrors import link_identity
                    from retirement_history import package_identity
                    with mutation.hold(), lock(self.collector/'pipeline.lock', blocking=False):
                        pointer = mutation.read()
                        if pointer['state'] == 'uncertain' and (mutation.owner != pointer.get('owner')
                                or str(journal.absolute()) != pointer.get('journal')):
                            return 'uncertain', ''
                        if source in read_history(journal)['acquisitions']:
                            return 'uncertain', ''
                        package = self.client.call('get_package_data', package_id=entry['package_id'])
                        origins = self.catalog.working_set('retire', origin_refs(self.catalog, source), media=False)['origins']
                        original = {o['fid']: o for o in origins.values()
                                    if o['pid'] == entry['package_id'] and not o.get('parent_origin')}
                        if (package['pid'] != entry['package_id'] or package['name'] != entry['name']
                                or not package['name'].startswith('Booksys-') or not original
                                or {f['fid'] for f in package['links']} != set(original)
                                or sorted(link_identity(f['url']) for f in package['links']) !=
                                   sorted(link_identity(u) for u in entry['links'])
                                or any(original[f['fid']]['path'] != str(Path(f.get('input_folder', package['folder']))/f['name'])
                                       or original[f['fid']].get('transfer_status') != f['status'] for f in package['links'])):
                            return 'uncertain', ''
                        proof = 'retirement-no-intent:' + digest({'entry': entry, 'package': package_identity(package)})
                        if pointer['state'] == 'uncertain':
                            mutation.resolve(proof)
                        return 'absent', proof
                if (record.get('phase') in ('deleting','retired')
                        and (not task.superseded_by or record.get('task_attempt') == [task.key, task.generation])
                        and (not task.source or record.get('submission') == entry)
                        and (pointer['state']!='uncertain' or
                             (mutation.owner == pointer.get('owner') and str(journal.absolute()) == pointer.get('journal')))):
                    result = Retirement(self.client, self.catalog, self.reception, self.collector/'monitor.yaml',
                        self.collector/'state.yaml', scripts=configured_scripts(), concurrent=True).process(source, apply=True,completions=completions,receipt_only=record)
                    if result.get('outcome') == 'retired':
                        if task.superseded_by:
                            if not completions:
                                completions.append(CollectorState(journal.parent).prepare_completion('retirement',source,record))
                            proof = 'retirement-readback:' + digest(completions[0].value)
                            with mutation.hold():
                                mutation.resolve(proof)
                            return 'done', proof
                        return 'done', 'retirement-readback:' + digest(result)
            elif task.stage == 'routing':
                source, entry = self._readback_binding(task)
                journal = self.collector/'mirror-routing.yaml'
                mutation = NativeMutation(budget.path.parent, 'routing:' + source, journal)
                if pointer['state']=='uncertain' and (mutation.owner != pointer.get('owner') or str(journal.absolute()) != pointer.get('journal')):
                    return 'uncertain', ''
                from mirror_routing import owned_package, package_signature
                from library.primitives import atomic_yaml
                with mutation.hold(), lock(journal.with_suffix('.lock')):
                    value = NativeHistory(journal.parent).routing()
                    operation = value['packages'][str(entry['package_id'])]
                    if task.superseded_by and operation.get('task_attempt') != [task.key, task.generation]:
                        return 'uncertain', ''
                    package = owned_package(self.client, entry)
                    if (operation.get('source') != source or package_signature(package) != operation['original_files']):
                        return 'uncertain', ''
                    phase = operation['phase']
                    if phase == 'intent' and package['dest'] == 0:
                        operation['phase'] = 'parked'
                    elif phase == 'parked' and not any(item['package_id'] == package['pid']
                                                      for item in self.client.call('status_downloads')):
                        operation['phase'] = 'stopped'
                    elif phase=='stopped' and operation['kind']=='park':
                        if package['dest']!=0 or any(item['package_id']==package['pid'] for item in self.client.call('status_downloads')):
                            return 'uncertain',''
                    elif phase in ('create_intent', 'attempt_created', 'attempt_verified','active'):
                        from collections import Counter
                        from mirrors import link_identity
                        matches = [p for p in self.client.call('get_queue_data') + self.client.call('get_collector_data')
                                   if p['name'] == operation['attempt_name']]
                        if len(matches) != 1:
                            return 'uncertain', ''  # Absence never authorizes another create.
                        attempt = matches[0]
                        expected = Counter(link_identity(url) for url in operation['fallback_links'])
                        if (Counter(link_identity(item['url']) for item in attempt['links']) != expected
                                or attempt['folder'] == operation['original_folder']
                                or any(item['fid'] in {fid for fid, _ in operation['original_files']} for item in attempt['links'])):
                            return 'uncertain', ''
                        if phase=='active' and (attempt['dest']!=1 or attempt['pid']!=operation.get('attempt_pid') or attempt['folder']!=operation.get('attempt_folder') or package_signature(attempt)!=operation.get('attempt_files')):
                            return 'uncertain',''
                        operation.update(attempt_pid=attempt['pid'], attempt_folder=attempt['folder'],
                                         attempt_files=package_signature(attempt),
                                         phase='active' if attempt['dest'] == 1 else 'attempt_created')
                    else:
                        return 'uncertain', ''
                    proof = 'routing-readback:' + digest(operation)
                    # Persist effect resolution before the journal continuation.
                    # A crash in between is safe: the native domain adapter
                    # repeats readback and never creates an unconfirmed package.
                    mutation.resolve(proof)
                    if operation['phase']=='active' or operation['phase']=='stopped' and operation['kind']=='park':
                        completions.append(CollectorState(journal.parent).prepare_completion('routing',str(entry['package_id']),dict(operation)))
                        return 'done',proof
                    NativeHistory(journal.parent).route(str(entry['package_id']),operation)
                    return 'absent', proof
        except (CollectionError, LibraryError, OSError, KeyError, ValueError, TypeError):
            return 'uncertain', ''
        return 'uncertain', ''
