"""Scoped native-source relationship lookup through the catalogue facade."""
from __future__ import annotations
from library.catalog import Catalog
from library.primitives import LibraryError

def origin_refs(catalog: Catalog, source: str) -> dict[str, list[str]]:
    backend = catalog.backend()
    if backend is None:
        raise LibraryError('Independent stages require PostgreSQL authority')
    refs: dict[str, list[str]] = {'origins': []}
    after = ''
    while True:
        page = backend.scan('origins', filters={'source_ref': source}, after=after, limit=100)
        refs['origins'].extend(ident for ident, _ in page)
        if len(refs['origins']) > 4096:
            raise LibraryError('Acquisition relationship capacity exceeded')
        if len(page) < 100:
            return refs
        after = page[-1][0]
