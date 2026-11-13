"""Terminal acquisition evidence. Retained packages are explicitly historical."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


from booksys_core.errors import CollectionError


def history_path(routing: Path) -> Path:
    return routing.parent / 'package-retirement.yaml'


def evidence_digest(record: dict[str, Any]) -> str:
    payload = {k: record[k] for k in ('source', 'submission', 'packages', 'origins', 'routing', 'files', 'unrelated')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def package_identity(package: dict[str, Any]) -> dict[str, Any]:
    return {k: package[k] for k in ('pid', 'name', 'folder')} | {
        'links': sorted([[f['fid'], f['url'], f['name'], f['status']] for f in package['links']])}


def same_package(retained: dict[str, Any], live: dict[str, Any]) -> bool:
    """pyLoad gives a deleted package's id to a later package. Name and link set tell them apart."""
    def links(package: dict[str, Any]) -> list[tuple[Any, Any]]:
        return sorted((f['fid'], f['url']) for f in package['links'])
    return retained.get('name') == live.get('name') and links(retained) == links(live)


def read_history(path: Path) -> dict[str, Any]:
    from library.operational_state import NativeHistory
    value = NativeHistory(path.parent).retirements()
    return validate_history(value)


def validate_history(value: dict[str,Any]) -> dict[str,Any]:
    """Validate the same historical contract for runtime reads and offline import."""
    if (not isinstance(value, dict) or value.get('schema') != 1
            or not isinstance(value.get('acquisitions'), dict)):
        raise CollectionError('Invalid retirement history')
    for source, record in value['acquisitions'].items():
        if (not isinstance(source, str) or not isinstance(record, dict)
                or record.get('phase') not in ('prepared', 'deleting', 'retired')
                or not isinstance(record.get('packages'), dict)):
            raise CollectionError('Invalid retirement record')
        for key, package in record['packages'].items():
            if (not isinstance(package, dict) or type(package.get('pid')) is not int
                    or str(package['pid']) != key or package['pid'] <= 0
                    or not package.get('name', '').startswith('Booksys-')
                    or not isinstance(package.get('links'), list)):
                raise CollectionError('Invalid retained package')
        try:
            if record.get('evidence_digest') != evidence_digest(record):
                raise CollectionError('Retirement evidence changed')
        except (KeyError, TypeError) as exc:
            raise CollectionError('Incomplete retirement evidence') from exc
    return dict(value)


def retained_snapshot(packages: list[dict[str, Any]], path: Path) -> list[dict[str, Any]]:
    result = copy.deepcopy(packages)
    live = {p['pid']: p for p in result}
    kept: dict[Any, dict[str, Any]] = {}
    for source, record in read_history(path)['acquisitions'].items():
        if record['phase'] == 'prepared':
            continue
        for package in record['packages'].values():
            if package['pid'] in live:
                if not same_package(package, live[package['pid']]):
                    continue  # another package took the id after the delete; the retained one is gone
                if package_identity(package) != package_identity(live[package['pid']]):
                    raise CollectionError('Retirement target changed; inspect before proceeding')
                if record['phase'] == 'retired':
                    raise CollectionError('Retired pyLoad ID reappeared; inspect before proceeding')
                continue
            retained = copy.deepcopy(package)
            retained.update(retirement=record['phase'], retirement_source=source,
                            retirement_recorded_at=record['created_at'], live=False,
                            retirement_proof={'origins': record['origins'], 'files': record['files']})
            older = kept.get(package['pid'])
            if older is None or str(older['retirement_recorded_at']) <= str(retained['retirement_recorded_at']):
                kept[package['pid']] = retained  # pyLoad reuses ids: one retained package per id, the newest record
    result.extend(kept.values())
    return result
