"""Read-only operator JSONL search over the same bounded source reader as HTTP."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path

from booksys_core.diagnostics import utc
from library.diagnostic_reader import DiagnosticReader, Filter


def utc_epoch(value: str) -> float:
    if not utc(value):
        raise ValueError('UTC timestamp required')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def main(argv: list[str] | None = None, *, reader: DiagnosticReader | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/data/booksys/library-lab'))
    parser.add_argument('--from', dest='start', required=True)
    parser.add_argument('--to', dest='end', required=True)
    for field in ('service', 'stage', 'reason', 'ref-kind', 'ref', 'cycle-id', 'cursor'):
        parser.add_argument('--' + field)
    parser.add_argument('--older', action='store_true', help='Allow one explicit retained window older than24h')
    parser.add_argument('--stack', action='store_true', help='Include filtered project stack locations')
    args = parser.parse_args(argv)
    try:
        query = Filter(start=utc_epoch(args.start), end=utc_epoch(args.end), service=args.service,
            stage=args.stage, reason=args.reason, ref_kind=args.ref_kind, ref=args.ref, cycle_id=args.cycle_id)
        if reader is None:
            from library.log_sources import LiveSource
            sources = [LiveSource(name) for name in ('worker', 'byparr', 'library')]
            for source in sources:
                source.refresh()
            reader = DiagnosticReader(sources, args.root / 'evidence/diagnostic-reader.lock')
        result = reader.query(query, cursor=args.cursor, older=args.older, stacks=args.stack)
        rows = result.pop('events')
        # First JSONL object is continuation/availability metadata, then events.
        print(json.dumps({'schema': 1, **result}, ensure_ascii=True, allow_nan=False))
        for row in rows:
            print(json.dumps(row, ensure_ascii=True, allow_nan=False))
        if result['state'] == 'partial' and not rows and len({gap['source'] for gap in result['gaps']
                if gap['reason'] == 'unavailable'}) == len(reader.sources):
            return 3
        return {'complete': 0, 'continued': 0, 'partial': 2, 'busy': 4}[result['state']]
    except (ValueError, TypeError, KeyError, OSError):
        print(json.dumps({'schema': 1, 'state': 'invalid', 'reason': 'Bounded diagnostic request failed'}))
        return 1
