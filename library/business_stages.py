"""Bounded asset steps. Metadata, printed evidence and CBZ admission stay separate."""
from __future__ import annotations

from dataclasses import replace
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
            keys = [key for key, _ in backend.scan('assets', after=after, limit=limit)]
        for key in keys:
            state = self.catalog.working_set('identify', {'assets': [key]}, media=False)
            if key not in state['assets']:
                continue
            for obligation in self.obligations(state, key, now=now):
                prior = state['assets'][key].get('stage_metadata', {})
                failures = prior.get('failed_dependencies', {})
                if (obligation.stage == 'metadata' and prior.get('outcome') == 'retry' and failures
                        and prior.get('evidence') == evidence_version(state['assets'][key])):
                    from library.dependency_recovery import DependencyRecovery
                    proof = DependencyRecovery(self.catalog.directory/'dependency-readiness.yaml',
                                               database=backend._database).proof(failures)
                    with backend._database.connection(write=True) as db:
                        current = db.execute('SELECT w.state,w.input_rev,m.evaluation FROM work_items w '
                                             'LEFT JOIN asset_metadata m ON m.asset=w.item '
                                             'WHERE w.stage=%s AND w.item=%s FOR UPDATE OF w',
                                             ('metadata', key)).fetchone()
                        if current and (current[0] == 'blocked' or current[2] != prior.get('evaluation')):
                            continue
                        # A crash can exhaust an episode before its result commits.
                        # Keep that revision even if readiness becomes unavailable.
                        if current and current[1].startswith('recovery:'):
                            obligation = replace(obligation, input_revision=current[1])
                        if current and current[0] == 'failed' and proof and proof != prior.get('recovery_proof'):
                            obligation = replace(obligation, input_revision='recovery:' + proof)
                        tasks._check(db)
                        tasks._discover(db, obligation, now=now)
                        count += 1
                        continue
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
            events = [event for event in self.providers.events[start:]
                      if not (event.get('dependency') == 'bdnex'
                              and asset.get('work_result', {}).get('status') == 'identified')]
            cooldowns = [event for event in events if event.get('reason') == 'BDneX cooldown']
            other_events = [event for event in events if event.get('reason') != 'BDneX cooldown']
            temporary = any(event.get('status') == 'unavailable' and (event.get('http_status', 0) == 429
                or event.get('http_status', 0) >= 500 or 'reason' in event and event['reason'] != 'Request budget exhausted')
                for event in other_events)
            budget_exhausted = any(event.get('reason') == 'Request budget exhausted' for event in other_events)
            outcome = ('defer' if budget_exhausted else 'retry' if temporary
                       else 'defer' if cooldowns else 'done')
            from booksys_core.diagnostics import provider_diagnostic
            diagnostic_events = cooldowns if outcome == 'defer' and not budget_exhausted else other_events
            diagnostic = provider_diagnostic(diagnostic_events, deferred=budget_exhausted) if outcome != 'done' else {}
            consumed_recovery = (task.input_revision.removeprefix('recovery:')
                                 if task.input_revision.startswith('recovery:')
                                 else asset.get('stage_metadata', {}).get('recovery_proof'))
            asset['stage_metadata'] = {'evidence': evidence_version(asset), 'at': instant(now),
                                      'evaluation': task.input_revision, 'outcome': outcome, 'method': task.method}
            failures = {str(event['dependency']): datetime.fromisoformat(event['retrieved_at']).timestamp()
                        for event in other_events if event.get('status') == 'unavailable'
                        and (event.get('http_status', 0) == 429 or event.get('http_status', 0) >= 500 or 'reason' in event)
                        and event.get('dependency') and event.get('retrieved_at')}
            if consumed_recovery:
                asset['stage_metadata']['recovery_proof'] = consumed_recovery
            if failures:
                asset['stage_metadata']['failed_dependencies'] = failures
            if outcome == 'done':
                asset['stage_metadata']['next_at'] = instant(now + timedelta(seconds=WEEK))
            prepared = self.catalog.prepare(state)
            if outcome == 'defer':
                client = self.providers.bdnex
                due = client.cooldown_deadline() if cooldowns and client is not None else now
                if budget_exhausted:
                    due = max(due, datetime.now(timezone.utc) + timedelta(seconds=30))
                    if self.providers.admission is not None:
                        timestamp = self.providers.admission.next_due()
                        if timestamp != float('inf'):
                            due = max(due, datetime.fromtimestamp(timestamp, timezone.utc))
                return StepResult('defer', due_at=due, prepared=prepared, diagnostic=diagnostic, successors=self.obligations(state, task.entity, now=now))
            return StepResult(outcome, prepared=prepared, diagnostic=diagnostic, successors=self.obligations(state, task.entity, now=now))
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
