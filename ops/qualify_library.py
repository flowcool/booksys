"""Bounded read-only provider qualification. Writes only the selected evidence file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import atomic_yaml, now
from library.cli import isolated_paths
from library.metadata import Providers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    isolated_paths(args.output)
    providers = Providers(args.output.parent / 'provider-cache', budget=12)
    value: dict[str, Any] = {'schema': 1, 'created_at': now(), 'platform': platform.platform(), 'results': {
        'french_bd_discovery': providers.discover('Asterix le Gaulois'),
        'manga_discovery': providers.discover('One Piece'),
        'manga_edition': providers.edition('9781569319017'),
        'french_bd_genres_public': providers.source('bdtheque', '27089/le-gang-de-la-clef-a-molette'),
        'manga_genres_public': providers.source('mangadex', 'a1c7c817-4e59-43b7-9365-09675a149a6f')}}
    attempts = providers.resolve_discovery(value['results']['manga_discovery'])
    candidates = [item['candidate'] for item in attempts if item['status'] == 'ok']
    value['results']['manga_discovery_editions'] = {'status': 'ok' if candidates else 'unavailable',
                                                  'candidates': candidates, 'attempts': attempts}
    atomic_yaml(args.output, value)
    print(json.dumps({key: {'status': item['status'], 'reason': item.get('reason')}
                      for key, item in value['results'].items()}, ensure_ascii=False))
    return 0 if all(item['status'] == 'ok' for item in value['results'].values()) else 2


if __name__ == '__main__':
    raise SystemExit(main())
