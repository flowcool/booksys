"""Restore retained validated downloads; dry-run unless --apply is explicit."""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.catalog import Catalog
from library.reception import reception_path
from library.readers import sha256
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--state', type=Path, required=True)
parser.add_argument('--downloads', type=Path, required=True)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args(); store = Catalog(args.state)
for key, asset in store.records('assets', media=False):
    state = store.working_set('admit', {'assets': [key]}, media=False); changed = False
    for oid in state['assets'][key]['origins']:
        origin = state['origins'][oid]; relative = Path(origin['path'])
        if origin.get('availability') == 'package_missing' and origin.get('sha256') == key and key and (path := reception_path(args.downloads, str(relative.parent), relative.name)).is_file() and sha256(path) == key:
            origin['availability'] = 'validated'; changed = True; print('apply' if args.apply else 'dry-run', oid, key)
    if args.apply and changed: store.save(state, expected=state['revision'])
