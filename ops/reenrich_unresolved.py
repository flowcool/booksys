"""Explicit operator re-enrichment through existing providers, with no task reset."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.automatic import evidence_version
from library.catalog import Catalog
from library.codec import encode
from library.metadata import Providers, enrich
from library.primitives import atomic_yaml
from library.stage_execution import QueueOwner, effect_scopes, record_scopes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    selected = json.loads(args.selection.read_text())['groups']['done_only']
    catalog = Catalog(args.state)
    backend = catalog.backend()
    assert backend is not None
    providers = Providers(args.state/'library-provider-cache', budget=50, shared=args.state)
    assert providers.admission is not None
    results: list[dict[str, Any]] = []
    with ExitStack() as stack:
        stack.enter_context(QueueOwner(args.state, 'metadata'))
        for name in ('library-cycle.lock', 'stage-checkpoint.lock'):
            boundary = stack.enter_context((args.state/name).open('a'))
            fcntl.flock(boundary, (fcntl.LOCK_EX if name == 'stage-checkpoint.lock' else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        for entry in selected:
            if providers.budget <= 0:
                break
            key = entry['asset']
            with backend._database.connection() as db:
                tasks = [json.loads(row[0]) for row in db.execute(
                    "SELECT payload FROM stage_tasks WHERE stage='metadata' AND payload::jsonb->>'entity'=%s", (key,))]
            before = catalog.get('assets', key)
            assert before is not None
            if (evidence_version(before) != entry['evidence'] or before.get('identity')
                    or before.get('work_result', {}).get('status') in ('identified', 'review', 'conflict', 'ambiguous')
                    or before.get('work_decision') or before.get('resolution') or before.get('overrides')
                    or any(task['status'] in ('active', 'pending', 'blocked') for task in tasks)):
                results.append({'asset': key, 'outcome': 'changed_or_ineligible'})
                continue
            state = catalog.working_set('identify', {'assets': [key]}, media=False)
            with effect_scopes(args.state, record_scopes(state)):
                atomic_yaml(args.evidence/(key+'-before.yaml'), before)
                atomic_yaml(args.evidence/'intent.yaml', {'asset': key, 'state': 'started',
                    'evidence': entry['evidence'], 'authority': 'the operator requested unresolved re-enrichment; private-work-item'})
                start = len(providers.events)
                value = enrich(catalog, key, providers, [])['assets'][key]
                assert value.get('work_decision') == before.get('work_decision')
                assert value.get('overrides') == before.get('overrides')
                atomic_yaml(args.evidence/(key+'-after.yaml'), value)
                result = {'asset': key, 'outcome': 'enriched',
                    'work_status': value.get('work_result', {}).get('status'),
                    'edition_status': value.get('identity_result', {}).get('status'),
                    'provider_events': providers.events[start:],
                    'changed': encode(before) != encode(value)}
                results.append(result)
                atomic_yaml(args.evidence/'intent.yaml', {'asset': key, 'state': 'completed'})
                atomic_yaml(args.evidence/'results.yaml', {'results': results, 'budget_remaining': providers.budget})
                if any(event.get('reason') == 'Request budget exhausted' for event in providers.events[start:]):
                    break
    atomic_yaml(args.evidence/'results.yaml', {'results': results, 'budget_remaining': providers.budget,
        'selected': len(selected), 'remaining': len(selected)-len(results)})
    print(json.dumps({'selected': len(selected), 'processed': len(results), 'budget_remaining': providers.budget}))


if __name__ == '__main__':
    main()
