"""Readiness transitions from actual I/O, independent of worker lifetimes."""
from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Any
from library.postgres import Database, guards

from library.primitives import LibraryError, digest


class DependencyRecovery:
    def __init__(self, path: Path, *, database: Database | None = None):
        self.path = path
        self._database = database or Database.configured(path.parent)

    @classmethod
    def create(cls, path: Path, *, database: Database | None = None) -> DependencyRecovery:
        # Empty observations are created only by the explicit schema initializer.
        return cls(path,database=database)

    def read(self) -> dict[str, Any]:
        with self._database.connection() as db:
            rows = db.execute('SELECT name,available,at,recovered,url,next_probe FROM dependency_readiness ORDER BY name').fetchall()
        if len(rows)>64:
            raise LibraryError('Dependency readiness authority capacity')
        return {'schema':1,'dependencies':{name:{'available':available,'at':at,'recovered':recovered,
            'url':url,'next_probe':probe} for name,available,at,recovered,url,probe in rows}}

    def observe(self, name: str, *, available: bool, at: float, url: str = '') -> None:
        if not re.fullmatch('[a-z0-9.-]{1,128}', name) or type(available) is not bool or not math.isfinite(at) or at < 0:
            raise LibraryError('Invalid readiness observation')
        with self._database.connection(write=True) as db:
            guards(db, {'dependency:' + name})
            row = db.execute('SELECT available,at,recovered,url FROM dependency_readiness WHERE name=%s',(name,)).fetchone()
            if row is not None and at <= row[1]:
                return
            recovered = at if available and row is not None and row[0] is False else row[2] if row else 0
            db.execute('INSERT INTO dependency_readiness(name,available,at,recovered,url) VALUES (%s,%s,%s,%s,%s) '
                       'ON CONFLICT(name) DO UPDATE SET available=excluded.available,at=excluded.at,recovered=excluded.recovered,url=excluded.url',
                       (name,available,at,recovered,url or (row[3] if row else '')))

    def proof(self, failures: dict[str, float]) -> str:
        if not failures:
            return ''
        value = self.read()['dependencies']
        proof = {}
        for name, at in failures.items():
            current = value.get(name, {})
            if not current.get('available') or current.get('recovered', 0) <= at:
                return ''
            proof[name] = current['recovered']
        return digest(proof)

    def probe(self, providers: Any, *, at: float) -> None:
        """One failed, allowlisted provider request per bounded observation pass.

        Providers owns cache policy, shared budgets, deadlines and URL validation.
        A cached response cannot create a new readiness transition.
        """
        with self._database.connection(write=True) as db:
            row = db.execute('SELECT name,url FROM dependency_readiness WHERE NOT available AND url<>%s AND next_probe<=%s '
                             'ORDER BY next_probe,name FOR UPDATE SKIP LOCKED LIMIT 1',('',at)).fetchone()
            if row is None:
                return
            db.execute('UPDATE dependency_readiness SET next_probe=%s WHERE name=%s',(at+60,row[0]))
            url = row[1]
        providers.get(url)
