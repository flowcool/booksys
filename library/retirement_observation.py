"""Read-only retirement summary, decoded outside the latency-bounded API process."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from booksys_core.process import run_child


def project(path: Path) -> dict[str, Any]:
    from library.operational_state import NativeHistory
    value=NativeHistory(path.parent).retirements()
    history = value.get('acquisitions')
    if not isinstance(history, dict):
        return {}
    # Evidence, filenames, URLs and original snapshots stay in the journal.
    # These are exactly the fields consumed by Relationships and Activity.
    return {'acquisitions': {source: {'phase': record.get('phase'), 'packages': {
        key: {'pid': package['pid'], 'links': [[link['fid'] if isinstance(link, dict) else link[0]]
                                             for link in package.get('links', [])]}
        for key, package in record.get('packages', {}).items()}}
        for source, record in history.items()}}


def observe(path: Path) -> dict[str, Any]:
    try:
        result = run_child([sys.executable, '-m', 'library.retirement_observation', str(path)],
                           on_event=lambda event: None, timeout=20, stdout_limit=2 * 1024**2)
        if result.returncode:
            return {}
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TimeoutError):
        return {}  # Unavailable observation; never infer an empty history.


if __name__ == '__main__':
    try:
        print(json.dumps(project(Path(sys.argv[1])), ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')))
    except Exception:
        sys.exit(1)  # No untrusted journal contents on diagnostic stderr.
