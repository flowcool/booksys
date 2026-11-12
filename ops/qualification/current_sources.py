"""Measure native discovery on retained copies; never execute business work."""
from __future__ import annotations
import argparse
import copy
import yaml
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from library.catalog import empty
from library.migration import import_catalogue
from library.source_tasks import SourcePolicy
from library.stage_tasks import StageTasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--multiply', type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    root = args.evidence
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    collector = root/'collector'
    collector.mkdir(mode=0o700)
    hashes = {}
    for name in ('monitor.yaml', 'state.yaml', 'fiches.yaml', 'backfill.yaml', 'mirror-routing.yaml', 'package-retirement.yaml'):
        path = args.collector/name
        if path.exists():
            raw = path.read_bytes()
            (collector/name).write_bytes(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
    if args.multiply == 2:
        path = collector/'monitor.yaml'
        value = yaml.safe_load(path.read_bytes())
        value['submissions'].update({source+'-load-copy-2': copy.deepcopy(record) for source, record in list(value['submissions'].items())})
        path.write_text(yaml.safe_dump(value))
    store = import_catalogue(root/'state', empty(), authority='source-proof', source_digest='b'*64)
    StageTasks.upgrade(store)
    policy = SourcePolicy.create(root/'state/task-policy.yaml')
    tasks = StageTasks(store, policy=policy)
    result: dict[str, Any] = {'multiply': args.multiply, 'inputs': hashes, 'queues': {}}
    for queue in ('collection', 'routing', 'retirement'):
        count = 0
        started = time.monotonic()
        while True:
            scan = tasks.scan(queue, collector/'monitor.yaml', now=datetime.now(timezone.utc))
            count += scan.processed
            if not scan.continued or scan.blocked:
                result['queues'][queue] = {'tasks': count, 'blocked': scan.blocked, 'seconds': time.monotonic()-started,
                                          'policy_bytes': policy.path.stat().st_size}
                break
    (root/'result.json').write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    main()
