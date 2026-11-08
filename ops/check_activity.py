"""Read-only live Activity acceptance. Evidence contains only allowlisted observations."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Any
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from activity import read_yaml


def get(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=5) as response:
        value: dict[str, Any] = json.load(response)
        return value


def proof() -> dict[str, Any]:
    collector = Path('/data/booksys/state')
    submissions = read_yaml(collector / 'monitor.yaml').get('submissions', {})
    history = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (collector / 'backfill-history').glob('*.yaml')}
    retirements = read_yaml(collector / 'package-retirement.yaml').get('acquisitions', {})
    catalogue = read_yaml(Path('/data/booksys/library-lab/state/library.yaml'), max_bytes=128 * 1024 * 1024)
    if not isinstance(catalogue.get('origins'), dict) or not isinstance(catalogue.get('assets'), dict):
        raise RuntimeError('Canonical catalogue observation unavailable')
    origins = {k: {'sha256': v.get('sha256'), 'source': hashlib.sha256(v.get('source', '').encode()).hexdigest()}
               for k, v in catalogue.get('origins', {}).items()}
    output = subprocess.run(['docker', 'inspect', '--format', '{{.Name}} {{.Id}} {{.Image}} {{.State.Running}}',
                             'booksys-worker-1', 'booksys-byparr-1', 'booksys-pyload'], capture_output=True, text=True, check=True).stdout
    return {'submission_ids': {hashlib.sha256(s.encode()).hexdigest(): v.get('package_id') for s, v in submissions.items()},
            'backfill_history': history, 'retirements': {hashlib.sha256(s.encode()).hexdigest(): v['phase'] for s, v in retirements.items()},
            'origins': origins, 'containers': output.splitlines()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--url', default='http://192.0.2.10:8768')
    parser.add_argument('--before', action='store_true')
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    if args.before:
        (args.evidence / 'preservation-before.json').write_text(json.dumps(proof(), indent=2))
        return
    for _ in range(15):
        if get(args.url + '/api/activity').get('observed_at') is not None:
            break
        time.sleep(2)
    else:
        raise RuntimeError('Shared observer did not produce its first snapshot')
    samples = []
    for index in range(2):
        value = get(args.url + '/api/activity')
        samples.append(value)
        (args.evidence / ('activity-' + str(index) + '.json')).write_text(json.dumps(value, indent=2))
        if index == 0:
            time.sleep(36)
    if samples[1]['observed_at'] <= samples[0]['observed_at']:
        raise RuntimeError('Shared observer did not refresh')
    if any(v['freshness'] != 'current' for v in samples[1]['resources'].values()):
        raise RuntimeError('Resource observations are not current')
    if any(samples[1]['stages'][name]['queue_freshness'] != 'current' for name in ('local', 'identity')):
        raise RuntimeError('Library queue observations are not current')
    before = json.loads((args.evidence / 'preservation-before.json').read_text())
    after = proof()
    for field in ('submission_ids', 'backfill_history', 'origins'):
        for key, old in before[field].items():
            if key not in after[field]:
                raise RuntimeError('Prior ' + field + ' disappeared; inspect evidence')
            current = after[field][key]
            # Normal processing can assign a previously unknown package/hash.
            changed = (any(v is not None and current.get(k) != v for k, v in old.items())
                       if field == 'origins' else old is not None and current != old)
            if changed:
                raise RuntimeError('Prior known ' + field + ' changed; inspect evidence')
    if any(after['retirements'].get(k) != 'retired' for k, v in before['retirements'].items() if v == 'retired'):
        raise RuntimeError('Terminal retirement evidence changed')
    (args.evidence / 'preservation-after.json').write_text(json.dumps(after, indent=2))
    (args.evidence / 'acceptance.json').write_text(json.dumps({'refreshed': True, 'resources_current': True,
        'library_queues_current': True, 'old_submissions_preserved': True, 'old_origins_preserved': True, 'history_preserved': True}, indent=2))
    print('Activity refresh, resources and canonical preservation passed')


if __name__ == '__main__':
    main()
