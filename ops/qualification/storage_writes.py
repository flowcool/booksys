"""Measure ordinary targeted transactions on an isolated scaled catalogue."""
from __future__ import annotations
from contextlib import contextmanager
import argparse
import json
from pathlib import Path
import statistics
import time
from typing import Any, Iterator
from unittest.mock import patch
from library.sqlite_runtime import connection
from library.store import Store, Change


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.dataset/'candidate/state'
    if root.resolve().is_relative_to('/data/booksys'): raise ValueError('No live state')
    descriptor=json.loads((args.dataset/'dataset.json').read_text())
    store=Store(root,authority=descriptor['authority'])
    with connection(store.path) as db:
        aid=db.execute('SELECT source FROM asset_origin GROUP BY source HAVING count(*)=1 ORDER BY source LIMIT 1').fetchone()[0]
    durations: list[float]=[]; statements: list[int]=[]
    class Timed:
        def __init__(self, db: Any):
            self.db=db
            self.started: float | None=None
            self.count=0
        def __getattr__(self,name: str) -> Any:return getattr(self.db,name)
        def execute(self,sql: str,*values: Any) -> Any:
            result=self.db.execute(sql,*values)
            if sql == 'BEGIN IMMEDIATE':self.started=time.perf_counter();self.count=0
            elif self.started is not None:self.count+=1
            if sql == 'COMMIT' and self.started is not None:
                durations.append((time.perf_counter()-self.started)*1000);statements.append(self.count);self.started=None
            return result
    @contextmanager
    def measured(*values: Any,**options: Any) -> Iterator[Any]:
        with connection(*values,**options) as db:yield Timed(db)
    with patch('library.store.connection',new=measured):
        for number in range(20):
            record=store.get('assets',aid)
            if record is None:raise ValueError('Fixture asset required')
            record['qualification_counter']=number
            store.commit(Change(upserts={'assets':{aid:record}}),expected_revision=store.revision)
    result={'dataset':descriptor,'asset':aid,'transactions':len(durations),
        'median_ms':statistics.median(durations),'max_ms':max(durations),
        'over_100ms':[value for value in durations if value>=100], 'max_execute_statements':max(statements),
        'coverage':'BEGIN IMMEDIATE acquired through COMMIT including fsync; ordinary one-origin asset; no viewers'}
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:value for key,value in result.items() if key!='dataset'}))


if __name__=='__main__':main()
