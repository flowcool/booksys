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

    def discover(self, tasks: StageTasks, *, now: datetime, after: str = '', limit: int = 100, keys: list[str] | None = None) -> int:
        count = 0
        backend = self.catalog.backend()
        if backend is None:
            raise LibraryError('Independent stages require PostgreSQL authority')
        if keys is None:
            items = backend.scan('assets', after=after, limit=limit)
        else:                               # an incremental pass: only the assets written since the last one
            found = ((key, backend.get('assets', key)) for key in keys)
            items = [(key, record) for key, record in found if record is not None]
        for key, asset in items:
            state = self.catalog.working_set('identify', {'assets': [key]}, media=False)
            for obligation in self.obligations(state, key, now=now):
                tasks.discover(obligation, now=now)
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
                due_at=due if not changed else now))
        if (prior and asset.get('work_result', {}).get('status') != 'identified'
                and asset.get('printed', {}).get('status') != 'ok'):
            result.append(Obligation('ocr', key, digest({'asset': key, 'pages': asset.get('page_manifest'),
                'format': asset['format']}), METHODS['ocr']))
        desired = projection(state, key)
        if asset.get('admission', {}).get('signature') != desired['signature']:
            result.append(Obligation('copies', key, desired['signature'], METHODS['copies']))
        return result

    def execute(self, task: Task, *, now: datetime) -> str | StepResult:
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
        if task.stage == 'copies':
            from library.store import PreparedResult
            copy_results: list[PreparedResult]=[]
            writer = AdmissionPublisher(self.catalog, self.reception, self.library,
                capacity=self.capacity, isolated=self.isolated, lab=not self.isolated, local_recovery=True,prepared=copy_results)
            writer.publish_asset(task.entity)
            if len(copy_results)>1:
                raise LibraryError('Copies prepared multiple terminal records; preserve journal')
            return StepResult('done',prepared=copy_results[0] if copy_results else None)
        raise LibraryError('Stage requires its native source adapter')

    def receipt(self, task: Task) -> tuple[str,str] | StepResult:
        """Read the publication directory and its journal before another copy attempt.

        Reconciliation may finish an existing journal. It never starts a fresh
        conversion. Retained partial files remain available as evidence.
        """
        asset_id = task.entity
        if task.stage == 'copies':
            pending_results: list[Any]=[]
            writer=AdmissionPublisher(self.catalog,self.reception,self.library,capacity=self.capacity,isolated=self.isolated,lab=not self.isolated,local_recovery=True,prepared=pending_results)
            writer.reconcile(asset=asset_id)
            if len(pending_results)>1:raise LibraryError('Multiple recovered copy results require reconciliation')
            if pending_results:return StepResult('done',prepared=pending_results[0])
        asset = self.catalog.get('assets', asset_id)
        if asset is None:
            return 'absent', 'asset-absent:' + task.input_revision
        complete = asset.get('admission', {}).get('signature') == task.input_revision
        return ('done' if complete else 'absent'), 'local-outcome:' + digest(asset)
