"""Small collector journals and explicit derived exports."""
from __future__ import annotations
from pathlib import Path
import os
import re
import tempfile
from typing import Any
import yaml

def atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()

def export_links(records: dict[str, Any], directory: Path, hosters: set[str] | None = None) -> None:
    available = {entry["hoster"] for item in records.values() for entry in item["downloads"].values()}
    all_links = set()
    for hoster in sorted(hosters if hosters is not None else available):
        links = sorted({link for item in records.values()
                        for entry in item["downloads"].values() if entry["hoster"] == hoster
                        for link in entry["urls"]})
        all_links.update(links)
        slug = re.sub(r"[^a-z0-9-]+", "-", hoster.lower()).strip("-") or "unnamed"
        atomic_write(directory / f"{slug}.txt", "".join(link + "\n" for link in links))
    atomic_write(directory / "all-links.txt", "".join(link + "\n" for link in sorted(all_links)))


def save(path: Path, data: dict[str, Any]) -> None:
    if path.name in ('monitor.yaml','state.yaml','fiches.yaml','backfill.yaml'):
        from booksys_core.errors import CollectionError
        raise CollectionError('Operational source state requires PostgreSQL transitions')
    atomic_write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def monitor_patch(directory: Path, **changes: Any) -> None:
    """Merge the reserved collector fields through their canonical PostgreSQL rows."""
    from library.operational_state import CollectorState
    CollectorState(directory).patch_monitor(**changes)
