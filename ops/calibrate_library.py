"""Copy a bounded labelled source sample to isolated evidence; never promote it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import LibraryError, atomic_yaml, now
from library.cli import isolated_paths
from library.intake import reception_path
from library.metadata import Providers, identify, isbn13
from library.readers import fingerprint, inspect_file, sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--reception', type=Path, required=True)
    parser.add_argument('--file-ids', type=int, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    isolated_paths(args.output)
    if args.output.exists():
        raise LibraryError('Calibration output must be new')
    rows = yaml.safe_load(args.inventory.read_text())
    selected = [row for row in rows if row['fid'] in args.file_ids]
    if len(selected) != len(set(args.file_ids)) or len(selected) > 20 or sum(r['size'] for r in selected) > 2 * 1024**3:
        raise LibraryError('Sample selection is missing, duplicate or exceeds limits')
    args.output.mkdir(parents=True, mode=0o700)
    providers = Providers(args.output / 'provider-cache', budget=20)
    results = []
    for row in selected:
        source = reception_path(args.reception, row['folder'], row['name'])
        before = fingerprint(source)
        initial_hash = sha256(source)
        target = args.output / (str(row['fid']) + Path(row['name']).suffix.lower())
        shutil.copyfile(source, target)
        if fingerprint(source) != before or sha256(source) != initial_hash or sha256(target) != initial_hash:
            raise LibraryError('Sample changed during isolated copy')
        inspection = inspect_file(target, stable_seconds=.1)
        embedded = inspection.get('embedded', {})
        code = isbn13(str(embedded.get('ISBN', embedded.get('GTIN', ''))))
        candidates = []
        provider = providers.edition(code) if code else None
        if provider and provider['status'] == 'ok':
            candidates.append(provider['candidate'])
        identity = identify(embedded, candidates)
        # These are evidence labels, not inferred identities: no supplied edition
        # identifier/proof means that the only supported label is pending.
        label = 'pending_no_edition_evidence' if not code else 'manual_label_required'
        record = {'origin': row, 'asset': initial_hash, 'original_unchanged': True,
            'inspection': {k: v for k, v in inspection.items() if k != 'cover'},
            'identity': identity, 'label': label}
        results.append(record)
    value = {'schema': 1, 'time': now(), 'sample_count': len(results), 'results': results,
             'false_admissions_against_pending_labels': sum(r['identity']['status'] == 'identified'
                 and r['label'] == 'pending_no_edition_evidence' for r in results)}
    atomic_yaml(args.output / 'calibration.yaml', value)
    print(json.dumps({'samples': len(results), 'validated_files': sum(r['inspection']['status'] == 'validated' for r in results),
        'embedded_records': sum(bool(r['inspection'].get('embedded')) for r in results),
        'automatic_identities': sum(r['identity']['status'] == 'identified' for r in results),
        'labels': {label: sum(r['label'] == label for r in results) for label in ('pending_no_edition_evidence', 'manual_label_required')},
        'false_admissions': value['false_admissions_against_pending_labels']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
