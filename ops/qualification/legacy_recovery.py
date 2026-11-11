"""Run only against an isolated reverse export with the exact compatible checkout.

The caller sets PYTHONPATH to the archived legacy application. No downloader or
provider call is available here. Canonical files and catalogue values stay local.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--legacy', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if root.is_relative_to('/data/booksys') or not root.is_dir():
        raise ValueError('Legacy rehearsal requires existing isolated state')
    sys.path.insert(0, str(args.legacy.resolve()))
    from library.catalog import Catalog
    from library.publish import Publisher, Capacity
    from library.resolution import Resolution
    import library.catalog
    if Path(library.catalog.__file__).resolve().parent.parent != args.legacy.resolve():
        raise ValueError('Compatible code selection failed')
    catalog = Catalog(root / 'state')
    before = catalog.read()
    records = catalog.ledger()
    Resolution(catalog, root / 'reception').project()
    Publisher(catalog, root / 'reception', root / 'library', isolated=True,
              capacity=Capacity(reserve=0)).reconcile()
    after = catalog.read()
    if before != after:
        raise ValueError('Legacy reconciliation changed the recovered business state')
    raw = json.dumps(after, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    print(json.dumps({'schema': 1, 'semantic_json_sha256': hashlib.sha256(raw).hexdigest(),
                      'revision': after['revision'], 'decisions': len(records),
                      'legacy_reconciliation': 'unchanged', 'external_calls': 0}))


if __name__ == '__main__':
    main()
