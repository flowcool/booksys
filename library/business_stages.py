"""Bounded asset steps. Metadata, printed evidence and CBZ admission stay separate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from library.admission import AdmissionPublisher, eligible, projection
from library.automatic import evidence_version, WEEK, METHOD
from library.catalog import Catalog
from library.metadata import Providers, enrich
from library.primitives import LibraryError, digest
from library.publication import Capacity
from library.stage_model import Task, Obligation, StepResult, instant, METHODS
from library.stage_tasks import StageTasks


class BusinessStages:
    def __init__(self, catalog: Catalog, reception: Path, library: Path, providers: Providers,
                 *, capacity: Capacity | None = None, isolated: bool = False):
        self.catalog, self.reception, self.library = catalog, reception, library
        self.providers, self.capacity, self.isolated = providers, capacity, isolated

    def discover(self, tasks: StageTasks, *, now: datetime, after: str = '', limit: int = 100) -> int:
        count = 0
        backend = self.catalog.backend()
        if backend is None:
            raise LibraryError('Independent stages require PostgreSQL authority')
        for key, asset in backend.scan('assets', after=after, limit=limit):
            current = asset.get('resolution', {})
            if current.get('status') == 'pending' and current.get('decision'):
                record = backend.decision(current['decision'])
                if record is None or record.get('target') != key or record.get('action') != 'resolve':
                    raise LibraryError('Pending decision binding is unavailable')
                tasks.discover(Obligation('copies', 'decision:' + record['id'], record['id'], METHODS['manual']), now=now)
                count += 1
            state = self.catalog.working_set('identify', {'assets': [key]}, media=False)
            for obligation in self.obligations(state, key, now=now):
                task = tasks.discover(obligation, now=now)
                if obligation.stage == 'metadata' and task.blocked == 'attempts_exhausted' and self.providers.admission is not None:
                    from library.dependency_recovery import DependencyRecovery
                    prior = state['assets'][key].get('stage_metadata', {})
                    proof = DependencyRecovery(self.providers.admission.path.parent/'dependency-readiness.yaml').proof(
                        prior.get('failed_dependencies', {}))
                    if proof:
                        tasks.recover_dependency(task.key, proof=proof, now=now)
                count += 1
        return count

    @staticmethod
    def obligations(state: dict[str, Any], key: str, *, now: datetime) -> list[Obligation]:
        """One domain policy for discovery and atomic completion successors."""
        asset = state['assets'][key]
        result: list[Obligation] = []
        if (not eligible(state, key) or not any(state['origins'][oid].get('availability') == 'validated'
                                              for oid in asset['origins'])):
            return []
        evidence = evidence_version(asset)
        prior = asset.get('stage_metadata', {})
        due = datetime.fromisoformat(prior['next_at']) if prior.get('next_at') else now
        changed = prior.get('evidence') != evidence
        legacy = asset.get('automation', {})
        if not prior and legacy.get('method') == METHOD and legacy.get('evidence') == evidence:
            due = datetime.fromtimestamp(legacy.get('next_attempt', 0), timezone.utc)
            changed = False
        method_changed = prior.get('method', METHODS['metadata']) != METHODS['metadata']
        if changed or due <= now or method_changed:
            revision = (prior['evaluation'] if not changed and (prior.get('outcome') in ('retry', 'defer') or method_changed) and prior.get('evaluation') else
                        digest({'evidence': evidence, 'evaluation': prior.get('next_at', '')}))
            result.append(Obligation('metadata', key, revision, METHODS['metadata'],
                due_at=due if not changed else now, replay=not bool(asset.get('identity'))))
        if (prior and not asset.get('identity') and asset.get('work_result', {}).get('status') != 'identified'
                and asset.get('printed', {}).get('status') != 'ok'):
            result.append(Obligation('ocr', key, digest({'asset': key, 'pages': asset.get('page_manifest'),
                'format': asset['format']}), METHODS['ocr']))
        desired = projection(state, key)
        if asset.get('admission', {}).get('signature') != desired['signature']:
            result.append(Obligation('copies', key, desired['signature'], METHODS['copies']))
        return result

    def scopes(self, task: Task) -> list[str]:
        from library.stage_execution import record_scopes
        asset = self.decision_asset(task) if task.entity.startswith('decision:') else task.entity
        return record_scopes(self.catalog.working_set('identify', {'assets': [asset]}, media=False))

    def execute(self, task: Task, *, now: datetime) -> str | StepResult:
        from library.stage_execution import effect_scopes
        with effect_scopes(self.catalog.directory, self.scopes(task)):
            return self._execute(task, now=now)

    def _execute(self, task: Task, *, now: datetime) -> str | StepResult:
        if task.stage == 'metadata':
            start = len(self.providers.events)
            from library.metadata import prepare_enrichment
            state = prepare_enrichment(self.catalog, task.entity, self.providers, [])
            asset = state['assets'][task.entity]
            temporary = any(event.get('status') == 'unavailable' and (event.get('http_status', 0) == 429
                or event.get('http_status', 0) >= 500 or 'reason' in event and event['reason'] != 'Request budget exhausted')
                for event in self.providers.events[start:])
            deferred = any(event.get('reason') == 'Request budget exhausted' for event in self.providers.events[start:])
            outcome = 'defer' if deferred else 'retry' if temporary else 'done'
            asset['stage_metadata'] = {'evidence': evidence_version(asset), 'at': instant(now),
                                      'evaluation': task.input_revision, 'outcome': outcome, 'method': task.method}
            failures = {str(event['dependency']): datetime.fromisoformat(event['retrieved_at']).timestamp()
                        for event in self.providers.events[start:] if event.get('status') == 'unavailable'
                        and (event.get('http_status', 0) == 429 or event.get('http_status', 0) >= 500 or 'reason' in event)
                        and event.get('dependency') and event.get('retrieved_at')}
            if failures:
                asset['stage_metadata']['failed_dependencies'] = failures
            if outcome == 'done':
                asset['stage_metadata']['next_at'] = instant(now + timedelta(seconds=WEEK))
            prepared = self.catalog.prepare(state)
            if deferred:
                # Cached evidence cannot improve before its expiry. Count the
                # delay from completion, rather than the selection timestamp.
                due = datetime.now(timezone.utc)+timedelta(seconds=30)
                import math
                for event in self.providers.events[start:]:
                    expiry = event.get('expires')
                    if (event.get('reason') == 'Request budget exhausted'
                            and isinstance(expiry, (int, float)) and math.isfinite(expiry)):
                        due = max(due, datetime.fromtimestamp(expiry, timezone.utc))
                if self.providers.admission is not None:
                    timestamp = self.providers.admission.next_due()
                    if timestamp != float('inf'):
                        due = max(due, datetime.fromtimestamp(timestamp, timezone.utc))
                return StepResult('defer', due_at=due, prepared=prepared, successors=self.obligations(state, task.entity, now=now))
            return StepResult(outcome, prepared=prepared, successors=self.obligations(state, task.entity, now=now))
        if task.stage == 'ocr':
            from library.printed import prepare_printed
            state, result = prepare_printed(self.catalog, task.entity, self.reception)
            return StepResult('done' if result['status'] == 'ok' else 'retry',
                              prepared=self.catalog.prepare(state), successors=self.obligations(state, task.entity, now=now))
        if task.stage == 'copies' and task.entity.startswith('decision:'):
            from library.resolution import Resolution
            from library.publish import Publisher
            decision_results: list[Any]=[]
            decision_writer = Publisher(self.catalog, self.reception, self.library, capacity=self.capacity,
                isolated=self.isolated, lab=not self.isolated, local_recovery=True,prepared=decision_results)
            Resolution(self.catalog,self.reception).apply(decision_writer,limit=1,asset_only=self.decision_asset(task),task=task)
            if len(decision_results)>1:raise LibraryError('Multiple decision results require reconciliation')
            return StepResult('done',prepared=decision_results[0] if decision_results else None)
        if task.stage == 'copies':
            from library.store import PreparedResult
            copy_results: list[PreparedResult]=[]
            writer = AdmissionPublisher(self.catalog, self.reception, self.library,
                capacity=self.capacity, isolated=self.isolated, lab=not self.isolated, local_recovery=True,prepared=copy_results)
            result=writer.publish_asset(task.entity)
            if len(copy_results)>1:
                raise LibraryError('Copies prepared multiple terminal records; preserve journal')
            return StepResult('done',proof='copies:'+digest(result),prepared=copy_results[0] if copy_results else None)
        raise LibraryError('Stage requires its native source adapter')

    def local_failure(self, task: Task) -> str:
        if task.stage != 'copies':
            return ''
        from library.publish import Publisher
        from library.provisional import ProvisionalPublisher
        asset = self.decision_asset(task) if task.entity.startswith('decision:') else task.entity
        try:
            owned = []
            for writer_type in (Publisher, AdmissionPublisher, ProvisionalPublisher):
                writer = writer_type(self.catalog, self.reception, self.library, capacity=self.capacity,
                    isolated=self.isolated, lab=not self.isolated, local_recovery=True)
                for operation in writer.operations.records():
                    # Validate every namespace before declaring a local scope.
                    if writer.operation_applies(operation, asset):
                        owned.append(operation)
            return 'local-journals:' + digest(owned) if owned else ''
        except (LibraryError, OSError, KeyError, ValueError, TypeError):
            return ''

    def receipt(self, task: Task) -> tuple[str,str] | StepResult:
        """Recover canonical local outcomes after acting-process exclusion.

        Reconciliation may finish an existing journal. It never starts a fresh
        conversion. Retained partial files remain available as evidence.
        """
        asset_id = self.decision_asset(task) if task.entity.startswith('decision:') else task.entity
        if task.stage == 'copies':
            from library.publish import Publisher
            pending_results: list[Any]=[]
            writer=Publisher(self.catalog,self.reception,self.library,capacity=self.capacity,isolated=self.isolated,lab=not self.isolated,local_recovery=True,prepared=pending_results)
            if task.entity.startswith('decision:'):
                from library.resolution import Resolution
                Resolution(self.catalog,self.reception).apply(writer,limit=1,asset_only=asset_id,task=task,reconcile_only=True)
            else:
                writer.reconcile(asset=asset_id)
            if len(pending_results)>1:raise LibraryError('Multiple recovered copy results require reconciliation')
            if pending_results:return StepResult('done',proof='retained-copy-result',prepared=pending_results[0])
        asset = self.catalog.get('assets', asset_id)
        if asset is None:
            return 'absent', 'asset-absent:' + task.input_revision
        if task.stage == 'metadata':
            prior = asset.get('stage_metadata', {})
            complete = prior.get('evaluation') == task.input_revision and prior.get('outcome') == 'done'
        elif task.stage == 'ocr':
            complete = asset.get('printed', {}).get('status') == 'ok'
        elif task.entity.startswith('decision:'):
            resolution = asset.get('resolution', {})
            complete = (resolution.get('decision') == task.entity.removeprefix('decision:')
                        and resolution.get('status') == 'applied')
        else:
            complete = asset.get('admission', {}).get('signature') == task.input_revision
        return ('done' if complete else 'absent'), 'local-outcome:' + digest(asset)

    def decision_asset(self, task: Task) -> str:
        backend = self.catalog.backend()
        if backend is None:
            raise LibraryError('Decision task requires PostgreSQL authority')
        record = backend.decision(task.entity.removeprefix('decision:'))
        if record is None or record.get('action') != 'resolve':
            raise LibraryError('Decision task binding changed')
        return str(record['target'])
