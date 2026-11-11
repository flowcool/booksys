"""Measure selection on a retained isolated discovery fixture, without effects."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import resource
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from library.store import Store
from library.stage_tasks import StageTasks
from library.source_tasks import SourcePolicy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    args = parser.parse_args()
    # The named synthetic authority prevents pointing this probe at the lab.
    state = args.fixture/'state'
    tasks = StageTasks(Store(state, authority='source-proof'), policy=SourcePolicy(state/'task-policy.yaml'))
    results = []
    for queue in ('routing', 'retirement'):
        started = time.monotonic()
        selected = tasks.select(queue, now=datetime.now(timezone.utc))
        results.append({'queue': queue, 'seconds': time.monotonic()-started,
                        'eligible': selected.eligible, 'selected': bool(selected.task)})
        if selected.eligible and not selected.task:
            raise RuntimeError('Eligible queue made no selection')
    (args.fixture/'selection.json').write_text(json.dumps({'queues': results,
        'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}, indent=2)+'\n')


if __name__ == '__main__':
    main()
