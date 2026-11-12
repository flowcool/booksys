"""One-screen health of the Booksys lab: what moves, what waits and why, what fails.

Run: uv run python ops/health.py
Read only. It shows the RAW wait reasons stored on the tasks, not the mapped log reasons,
so a recurring failure cannot hide behind "unknown".
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import psycopg

OP = Path('/data/booksys/postgresql-prfpw-20261111')
SLICE = Path('/sys/fs/cgroup/booksys.slice')


def sh(*argv: str) -> str:
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30).stdout  # docker logs writes app logs to stderr


def main() -> None:
    dsn = (OP / 'secrets/app.dsn').read_text().strip()
    with psycopg.connect(dsn, connect_timeout=10) as db:
        db.execute('SET search_path TO booksys')
        age = db.execute('SELECT round(extract(epoch from now())-observed_at) FROM native_observation').fetchone()
        retired = db.execute('SELECT count(*) FROM retirement_acquisitions').fetchone()
        work = []
        if db.execute("SELECT to_regclass('work_items')").fetchone()[0] is not None:
            work = db.execute("SELECT stage, count(*) FILTER (WHERE state='done' AND completed_at > now() - interval '60 minutes'), "
                "count(*) FILTER (WHERE state IN ('ready','waiting')), count(*) FILTER (WHERE state IN ('blocked','failed')), "
                "count(*) FILTER (WHERE state='claimed'), "
                "(SELECT string_agg(wait_reason||' '||n, '; ') FROM (SELECT wait_reason, count(*) n FROM work_items w2 WHERE w2.stage=w.stage "
                "AND state='waiting' AND wait_reason<>'' GROUP BY 1 ORDER BY 2 DESC LIMIT 3) x) "
                "FROM work_items w GROUP BY stage ORDER BY stage").fetchall()
        deleting = db.execute("SELECT count(*) FROM retirement_acquisitions WHERE payload::json->>'phase'='deleting'").fetchone()
        crawl = db.execute("SELECT COALESCE(payload::json->>'deep_page','6'), "
            "round(extract(epoch from now()-(payload::json->>'last_cycle')::timestamptz)) FROM collection_categories ORDER BY source LIMIT 1").fetchone()
    print(f'pyLoad snapshot age: {age[0] if age else "none"} s   retired packages: {retired[0] if retired else "?"}   sources in phase deleting: {deleting[0] if deleting else "?"}')
    pages = int(os.environ.get('BOOKSYS_CRAWL_PAGES', '5'))
    print(f'collection: crawling {pages} pages (BOOKSYS_CRAWL_PAGES), deep cursor page {crawl[0] if crawl else 6}, '
          f'last cycle {str(crawl[1]) + " s" if crawl and crawl[1] is not None else "unknown"} ago')
    if work:
        print('work items per stage')
        print(f'{"stage":11} {"done/60min":>10} {"waiting":>8} {"held":>6} {"claimed":>8}  top wait reasons')
        for stage, done, waiting, held, claimed, reasons_text in work:
            print(f'{stage:11} {done:10} {waiting:8} {held:6} {claimed:8}  {reasons_text or ""}')
    if age and age[0] is not None and age[0] > 300:
        print(f'!! SNAPSHOT OLD: {age[0]} s; stages that read the pyLoad queue wait for it')
    print()
    rows = sh('docker', 'ps', '-a', '--format', '{{.Names}}|{{.Status}}').splitlines()
    names = [r.split('|') for r in rows if ('prfpw' in r or 'pyload' in r or 'byparr' in r) and not r.split('|')[0].endswith('-web')]
    bad = [f'{n.replace("booksys-pg-prfpw-audit1-", "")} ({s.split("(")[-1].rstrip(")") if "(" in s else s})'
           for n, s in names if not s.startswith('Up') or 'unhealthy' in s]
    print(f'containers: {sum(s.startswith("Up") for _, s in names)} running of {len(names)}; not healthy or stopped: {", ".join(bad) or "none"}')
    try:
        events = {k: int(v) for k, v in (line.split() for line in (SLICE / 'memory.events').read_text().splitlines())}
        used = int((SLICE / 'memory.current').read_text()) // 2**20
        limit = int((SLICE / 'memory.max').read_text()) // 2**20
        print(f'memory: {used} of {limit} MiB, kernel OOM kills since the slice started: {events.get("oom_kill", 0)}')
    except OSError:
        pass
    print('errors in the last 30 minutes (ERROR or CRITICAL):')
    found = False
    for name, status in names:
        if not status.startswith('Up') or 'pyload' in name:
            continue
        counts: dict[str, int] = {}
        for line in sh('docker', 'logs', '--since', '30m', name).splitlines():
            if '"level":"ERROR"' in line or '"level":"CRITICAL"' in line:
                try:
                    event = json.loads(line)
                    key = f'{event.get("stage")}/{event.get("code")}/{event.get("reason")}'
                except ValueError:
                    key = 'unparsed'
                counts[key] = counts.get(key, 0) + 1
        for key, n in sorted(counts.items(), key=lambda item: -item[1])[:3]:
            found = True
            print(f'  {name.replace("booksys-pg-prfpw-audit1-", ""):16} {n:4} x {key}')
    if not found:
        print('  none')


if __name__ == '__main__':
    main()
