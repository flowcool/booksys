"""Pure chronological effects of canonical operator decisions."""
from __future__ import annotations
import copy
from typing import Any
from library.primitives import LibraryError, digest

def apply_effect(state: dict[str, Any], decision: dict[str, Any], *, replay: bool = False) -> None:
    if decision['action'] in ('resolve', 'resolution_result'):
        from library.decision_model import apply_resolution
        apply_resolution(state, decision)
        return
    action, target, values = decision['action'], decision['target'], decision['values']
    if target not in state['assets']:
        raise LibraryError('Decision target missing after restore; restore inventory first')
    asset = state['assets'][target]
    if action == 'identify_work':
        # Replaying an earlier work choice must not reject a later exact identity.
        # New work-only requests still require ownership resolution first.
        if not replay and (asset.get('identity') or asset.get('resolution')):
            raise LibraryError('Resolve existing edition ownership before work-only confirmation')
        if not values.get('work', {}).get('title') or not values.get('evidence'):
            raise LibraryError('Work confirmation requires a title and evidence')
        result = {'status': 'identified', 'work': copy.deepcopy(values['work']),
                  'edition_status': 'unknown', 'field_conflicts': {}, 'reason': 'Operator confirmed work',
                  'evidence': copy.deepcopy(values['evidence']), 'decision': decision.get('id')}
        asset['work_decision'] = result
        from library.work_identity import merge_work_decision
        asset['work_result'] = merge_work_decision(asset.get('work_result', {}), result)
    elif action == 'identify':
        work, edition = values['work'], values['edition']
        wid, eid = values['work_id'], values['edition_id']
        if any(not edition.get(k) for k in ('language', 'publisher', 'evidence')) or not work.get('title'):
            raise LibraryError('Manual identity requires work and edition evidence')
        claims = {v['edition'] for v in state['variants'].values() if v['asset'] == target}
        if not replay and claims and claims != {eid}:
            raise LibraryError('Asset ownership conflict')
        if not replay and eid in state['editions'] and state['editions'][eid]['work'] != wid:
            raise LibraryError('Edition ownership conflict')
        state['works'][wid] = copy.deepcopy(work)
        managed = {k: v for k, v in state['editions'].get(eid, {}).items() if k in ('selected', 'publication')}
        state['editions'][eid] = {**copy.deepcopy(edition), 'work': wid, **managed}
        vid = digest({'asset': target, 'edition': eid})
        state['variants'][vid] = {'edition': eid, 'asset': target}
        asset['identity'] = {'work': wid, 'edition': eid, 'variant': vid, 'evidence': edition['evidence']}
        asset['disposition'] = 'pending'
        conflict_inputs = digest({'identity_result': asset.get('identity_result'),
                                  'assertions': asset.get('assertions', [])})
        if not replay or decision.get('resolution_inputs') == conflict_inputs:
            asset['contested'] = False
    elif action == 'override':
        asset.setdefault('overrides', {}).update(values)
    elif action in ('exclude', 'defer'):
        asset['disposition'] = action
    elif action == 'select':
        identity = asset.get('identity')
        if not identity:
            raise LibraryError('Unknown edition cannot be selected')
        state['editions'][identity['edition']]['selected'] = identity['variant']
    elif action == 'merge_work':
        source = values['source_work_id']
        destination = values['work_id']
        if source not in state['works'] or destination not in state['works'] or source == destination:
            raise LibraryError('Merge requires two distinct known works')
        if not values.get('evidence'):
            raise LibraryError('Merge evidence is required')
        for edition in state['editions'].values():
            if edition['work'] == source:
                edition['work'] = destination
        for entry in state['assets'].values():
            if entry.get('identity', {}).get('work') == source:
                entry['identity']['work'] = destination
        state['works'][source]['merged_into'] = destination
    elif action == 'separate_edition':
        identity = asset.get('identity')
        if not identity or not values.get('evidence') or not values.get('work', {}).get('title'):
            raise LibraryError('Separation requires an edition and explicit work evidence')
        wid = values['work_id']
        state['works'][wid] = copy.deepcopy(values['work'])
        state['editions'][identity['edition']]['work'] = wid
        for entry in state['assets'].values():
            if entry.get('identity', {}).get('edition') == identity['edition']:
                entry['identity']['work'] = wid
    else:
        raise LibraryError('Unsupported decision action')
