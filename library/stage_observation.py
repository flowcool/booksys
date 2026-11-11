"""Bounded queue summaries. Observation never selects, repairs or dispatches work."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import math
import re

import yaml

from library.primitives import LibraryError
from library.source_tasks import bounded_read
from library.operational_state import RuntimeObservation
from library.stage_model import QUEUES, instant
from library.stage_resources import Resources
from library.stage_tasks import StageTasks

COUNTS = ('eligible', 'resource_wait', 'deferred', 'blocked', 'active', 'done')
RESOURCE_WAITS = ('resource_wait', 'older_waiter', 'output_capacity', 'conflict_wait')


def public_queues(raw: dict[str, Any], *, timestamp: float) -> dict[str, Any]:
    result = {}
    for queue in QUEUES:
        value = raw.get(queue)
        if not isinstance(value, dict):
            continue
        entry: dict[str, Any] = {}
        for key in ('observed_at', 'actor_observed_at', 'last_progress_at', 'oldest_eligible_at', 'next_due_at'):
            number = value.get(key)
            entry[key] = number if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0 else None
        for source, target in (('observed_at', 'freshness'), ('actor_observed_at', 'actor_freshness')):
            at = entry[source]
            entry[target] = 'missing' if at is None else 'current' if 0 <= timestamp-at <= 90 else 'stale'
        counts = value.get('counts', {})
        entry['counts'] = {name: count for name, count in (counts.items() if isinstance(counts, dict) else [])
                           if name in COUNTS and type(count) is int and count >= 0}
        active = value.get('active_task')
        entry['active_task'] = active if isinstance(active, str) and re.fullmatch('[0-9a-f]{64}', active) else None
        reasons = value.get('resource_wait_reasons', [])
        entry['resource_wait_reasons'] = [reason for reason in reasons if reason in RESOURCE_WAITS] if isinstance(reasons, list) else []
        entry['unit'] = 'domain tasks'
        entry['active_limit'] = 1
        result[queue] = entry
    return result


def observe(tasks: StageTasks, resources: Resources, heartbeats: Path, *, now: datetime) -> dict[str, Any]:
    stamp = instant(now)
    waiters = resources.waiters()
    result: dict[str, Any] = {}
    pulses=RuntimeObservation(tasks.store._database).heartbeats()
    with tasks.store._database.connection() as db:
        tasks._check(db)
        for queue in QUEUES:
            row = db.execute("WITH tasks AS (SELECT *, coalesce(payload::jsonb->>'wait_reason','') AS wait_reason FROM stage_tasks WHERE stage=%s) "
                "SELECT count(*) FILTER(WHERE status='pending' AND blocked='' AND due<=%s AND (wait_reason='' OR wait_reason IN ('resource_wait','older_waiter','output_capacity','conflict_wait'))), "
                "count(*) FILTER(WHERE status='pending' AND blocked='' AND (due>%s OR wait_reason='network_budget_wait')), "
                "count(*) FILTER(WHERE status IN ('pending','blocked','active') AND (status='blocked' OR blocked<>'' OR (status='pending' AND wait_reason<>'' AND wait_reason NOT IN ('network_budget_wait','resource_wait','older_waiter','output_capacity','conflict_wait')))), "
                "count(*) FILTER(WHERE status='active'), count(*) FILTER(WHERE status='done'), "
                "min(CASE WHEN status='pending' AND blocked='' AND due<=%s AND (wait_reason='' OR wait_reason IN ('resource_wait','older_waiter','output_capacity','conflict_wait')) THEN age END), "
                "max(payload::jsonb->>'completed_at') FROM tasks",
                (queue, stamp, stamp, stamp)).fetchone()
            detail = db.execute("SELECT min(CASE WHEN status='pending' AND blocked='' AND due>%s THEN due END), max(CASE WHEN status='active' THEN key END) FROM stage_tasks WHERE stage=%s", (stamp, queue)).fetchone()
            if row is None or detail is None:
                raise LibraryError('Queue observation unavailable')
            reasons = [row[0] for row in db.execute("SELECT DISTINCT payload::jsonb->>'wait_reason' FROM stage_tasks WHERE stage=%s AND status='pending' AND blocked=''", (queue,)) if row[0] in RESOURCE_WAITS]
            waiting = 0
            for grant in waiters:
                key, generation = grant.rsplit(':', 1)
                item = db.execute("SELECT 1 FROM stage_tasks WHERE key=%s AND stage=%s AND status='pending' "
                    "AND blocked='' AND due<=%s AND coalesce(payload::jsonb->>'wait_reason','') IN ('','resource_wait','older_waiter','output_capacity','conflict_wait') "
                    "AND (payload::jsonb->>'generation')::bigint=%s", (key, queue, stamp, int(generation)-1)).fetchone()
                waiting += int(item is not None)
            heartbeat={'observed_at':pulses.get(queue)}
            counts = dict(zip(('eligible', 'deferred', 'blocked', 'active', 'done'), [int(n or 0) for n in row[:5]]))
            result[queue] = {'observed_at': now.timestamp(), 'actor_observed_at': heartbeat.get('observed_at'),
                'oldest_eligible_at': datetime.fromisoformat(row[5]).timestamp() if row[5] else None,
                'last_progress_at': datetime.fromisoformat(row[6]).timestamp() if row[6] else None,
                'next_due_at': datetime.fromisoformat(detail[0]).timestamp() if detail[0] else None,
                'active_task': detail[1], 'resource_wait_reasons': reasons,
                'counts': {**counts, 'resource_wait': waiting}}
    return public_queues(result, timestamp=now.timestamp())
