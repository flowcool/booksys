"""Replay frozen asset evidence against retained public references, without network."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from library.metadata import Providers
from library.work_enrichment import enrich_work
from library.work_identity import normalized


def replay_details(inputs: Path, output: Path) -> int:
    """Replay frozen result evidence only; never open archives, network or DB."""
    from library.work_enrichment import save_work_result
    from library.work_identity import evaluate_work
    rows = []
    for raw in json.loads(inputs.read_text())['rows']:
        previous = raw.get('work_result') or {}
        evidence = copy.deepcopy(previous.get('evidence', {}))
        evidence.pop('cover_confirmation', None)
        proposed = evaluate_work(evidence, previous.get('candidates', []))
        proposed['evidence'] = evidence
        before = proposed['status']
        asset = {'sha256': raw['id']}
        save_work_result(asset, proposed)
        receipt = proposed['evidence'].get('cover_confirmation', {})
        rows.append({'asset': raw['id'], 'recorded': raw['status'], 'proposed': before,
                     'status': proposed['status'], 'reason': proposed['reason'],
                     'cover_result': receipt.get('result'), 'cover_reason': receipt.get('reason')})
    report = {'mode': 'frozen evidence replay; absent archive/cover inputs remain review; no DB/network',
        'inputs_sha256': hashlib.sha256(inputs.read_bytes()).hexdigest(), 'asset_count': len(rows),
        'recorded': dict(Counter(row['recorded'] for row in rows)),
        'proposed': dict(Counter(row['proposed'] for row in rows)),
        'statuses': dict(Counter(row['status'] for row in rows)),
        'cover_results': dict(Counter(row['cover_result'] for row in rows if row['cover_result'])), 'rows': rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'rows'}))
    return 0


def snapshot_state(assets: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {'assets': {}, 'origins': {}}
    for key, raw in assets.items():
        asset = copy.deepcopy(raw)
        origins = asset.pop('origins', [])
        asset.update(sha256=key, origins=[])
        state['assets'][key] = asset
        for index, origin in enumerate(origins):
            oid = key + '-' + str(index)
            if origin.get('parent_origin'):
                matches = [source.rsplit('#bundle-member=', 1)[1]
                    for source, hint in asset.get('captured_hints', {}).items()
                    if '#bundle-member=' in source and hint.get('parent_source') == origin['source']]
                if len(matches) == 1:
                    oid = matches[0]
            origin = {**origin, 'sha256': key}
            asset['origins'].append(oid)
            state['origins'][oid] = origin
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--frozen-details', action='store_true')
    parser.add_argument('--references', type=Path)
    parser.add_argument('--labels', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.frozen_details:
        return replay_details(args.inputs, args.output)
    if args.references is None or args.labels is None:
        parser.error('--references and --labels are required unless --frozen-details is set')
    inputs = json.loads(args.inputs.read_text())
    state = snapshot_state(inputs['assets'])
    references = {}
    for path in args.references.glob('*.yaml'):
        page = yaml.safe_load(path.read_text())
        if isinstance(page, dict) and 'url' in page:
            references[page['url']] = page
    missing: set[str] = set()
    def fetch(url: str) -> tuple[int, str]:
        page = references.get(url)
        if page and page.get('status') == 'ok':
            return 200, page['body']
        missing.add(url)
        return 503, ''
    labels = {row['asset_sha256']: row for row in json.loads(args.labels.read_text())}
    statuses: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    metrics: Counter[str] = Counter()
    rows = []
    with tempfile.TemporaryDirectory(prefix='booksys-work-replay-') as folder:
        providers = Providers(Path(folder), fetch=fetch, interval=0, budget=10000)
        for key, asset in state['assets'].items():
            enrich_work(state, key, providers)
            result = asset['work_result']
            statuses[result['status']] += 1
            row = {'asset': key, 'status': result['status'], 'reason': result['reason'],
                   'source_status': result.get('source_status'), 'work': result.get('work', {})}
            if result['status'] == 'identified':
                coverage.update(field for field, value in result['work'].items() if value is not None and value != '' and value != [])
                label = labels.get(key)
                if label:
                    expected = label['expected_work']
                    if isinstance(expected, str):
                        correct = normalized(expected, 'title') == normalized(result['work']['title'], 'title')
                    else:
                        correct = all(normalized(result['work'].get(f, ''), f) == normalized(v, f) for f, v in expected.items() if v is not None)
                    metrics['correct_labelled' if correct else 'wrong_labelled'] += 1
                    row['label_verdict'] = 'correct' if correct else 'wrong'
                else:
                    metrics['unlabelled_matches'] += 1
            rows.append(row)
    report = {'schema': 1, 'mode': 'offline retained-reference replay; no network; missing cache is unavailable',
        'inputs_sha256': hashlib.sha256(args.inputs.read_bytes()).hexdigest(),
        'input_manifest': inputs.get('manifest'), 'asset_count': len(rows), 'statuses': dict(statuses),
        'label_metrics': dict(metrics), 'field_coverage': dict(coverage), 'retained_references': len(references),
        'unavailable_reference_urls': sorted(missing), 'new_exact_edition_confirmations': 0,
        'baseline_exact_editions': 1, 'rows': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
