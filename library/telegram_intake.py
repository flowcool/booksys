"""Submit an explicit list of completed Telegram documents to external intake."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from booksys_core.errors import ResultInputsChanged
from library import intake
from library.bundle_worker import MAX_SOURCE
from library.catalog import Catalog
from library.primitives import LibraryError, digest, telegram_origin_folder, telegram_origin_id, valid_telegram_index_title
from library.readers import READER_POLICY, fingerprint, sha256
from library.stage_model import METHODS, Obligation

FORMATS = frozenset(('.cbz', '.cbr', '.pdf'))
MAX_ENTRIES = 4096
MAX_LINE = 64 * 1024


def _name(value: object) -> str:
    if (not isinstance(value, str) or not value or value.startswith(('.', 'ug-tmp'))
            or re.search(r'[/\\\x00-\x1f\x7f-\x9f]', value) or len(value.encode('utf-8')) > 255):
        raise LibraryError('Unsafe Telegram filename')
    if Path(value).suffix.lower() not in FORMATS:
        raise LibraryError('Unsupported Telegram format')
    if Path(value).stem.lower().endswith(('.part', '.partial', '.tmp', '.crdownload', '.download')):
        raise LibraryError('Partial Telegram filename')
    return value


def _scope(value: str) -> tuple[int, int | None]:
    if not re.fullmatch(r'[1-9][0-9]{0,18}(?::[1-9][0-9]{0,9})?', value):
        raise argparse.ArgumentTypeError('Use CHANNEL or CHANNEL:TOPIC with positive tdl IDs')
    channel, _, topic = value.partition(':')
    peer, thread = int(channel), int(topic) if topic else None
    if peer >= 2**63 or (thread is not None and thread >= 2**31):
        raise argparse.ArgumentTypeError('tdl channel or topic ID is too large')
    return peer, thread


def _origin(entry: Any, scopes: set[tuple[int, int | None]]) -> tuple[str, dict[str, Any]]:
    if not isinstance(entry, dict) or entry.get('completed') is not True:
        raise LibraryError('Telegram transfer must be completed')
    receipt = entry.get('receipt')
    if not isinstance(receipt, dict):
        raise LibraryError('Missing tdl receipt')
    channel, messages = receipt.get('id'), receipt.get('messages')
    topic = entry.get('topic')
    if (type(channel) is not int or 'topic' not in entry
            or (topic is not None and type(topic) is not int) or (channel, topic) not in scopes):
        raise LibraryError('Telegram channel and topic are outside the selected scope')
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
        raise LibraryError('A tdl receipt must name one document')
    document = messages[0]
    message = document.get('id')
    if type(message) is not int or not 0 < message < 2**31 or document.get('type', 'message') != 'message':
        raise LibraryError('Invalid Telegram message ID or type')

    raw_name = document.get('file')
    # Two retained tdl exports contain C1 control characters in display names.
    # Keep the original receipt private and use a safe form for catalogue provenance.
    original_name = _name(re.sub(r'[\x80-\x9f]', '_', raw_name) if isinstance(raw_name, str) else raw_name)
    relative = entry.get('path')
    folder = str(telegram_origin_folder(channel, message))
    if not isinstance(relative, str) or not relative.startswith(folder + '/') or '/' in relative[len(folder)+1:]:
        raise LibraryError('Telegram path must be relative to its managed message directory')
    name = _name(relative[len(folder)+1:])
    if Path(name).suffix.lower() != Path(original_name).suffix.lower():
        raise LibraryError('Telegram path and receipt formats differ')
    size, checksum = entry.get('size'), entry.get('sha256')
    if type(size) is not int or not 0 < size <= MAX_SOURCE:
        raise LibraryError('Invalid completed Telegram file size')
    if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum):
        raise LibraryError('Missing or invalid completed Telegram SHA-256')
    caption = document.get('text', '')
    if not isinstance(caption, str) or '\x00' in caption or len(caption.encode('utf-8')) > 16 * 1024:
        raise LibraryError('Invalid Telegram caption')
    path = intake.reception_path(intake.EXTERNAL_ROOT, folder, name)
    stamp = fingerprint(path)
    if size != stamp[2]:
        raise LibraryError('Telegram file size differs from the completed receipt')
    if sha256(path) != checksum or fingerprint(path) != stamp:
        raise LibraryError('Telegram file SHA-256 differs from the completed receipt or the file changed')
    provenance = {'channel': channel, 'message': message, 'topic': topic, 'original_name': original_name,
                  'sha256': checksum, 'receipt_sha256': digest(receipt), 'caption': caption}
    if 'index_title' in entry:
        index_title = entry['index_title']
        if not valid_telegram_index_title(index_title):
            raise LibraryError('Invalid Telegram index title')
        provenance['index_title'] = index_title
    return telegram_origin_id(channel, message), {'source': 'external:' + relative, 'pid': None, 'fid': None,
        'path': relative, 'size': size, 'sha256': None, 'availability': 'pending', 'telegram': provenance}


def _check_repeat(existing: dict[str, Any], proposed: dict[str, Any]) -> None:
    if any(existing.get(field) != proposed[field] for field in ('path', 'size', 'telegram')):
        raise LibraryError('Telegram receipt changed for an already submitted channel and message')


def _submit(catalog: Catalog, oid: str, origin: dict[str, Any], *, at: datetime) -> str:
    state = catalog.result_set('retire', {'origins': [oid]}, media=False)
    existing = state['origins'].get(oid)
    if existing is not None:
        _check_repeat(existing, origin)
        return 'already-submitted'
    state['origins'][oid] = origin
    result = catalog.prepare(state)
    obligation = Obligation('intake', 'external-' + oid, digest({'origin': oid, 'reader': READER_POLICY,
        'sha256': origin['telegram']['sha256']}), METHODS['intake'])
    try:
        catalog.backend().commit(result.change, expected_revision=result.revision, conflict=result.conflict,
                                 obligations=[obligation], now=at)
    except ResultInputsChanged:
        # Another submitter may have committed the same receipt since the snapshot.
        existing = catalog.get('origins', oid)
        if existing is None:
            raise
        _check_repeat(existing, origin)
        return 'already-submitted'
    return 'submitted'


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, help='JSON Lines list of completed documents')
    parser.add_argument('--scope', action='append', required=True, type=_scope, help='Approved CHANNEL[:TOPIC]')
    parser.add_argument('--state-dir', type=Path, help='Worker catalogue state directory')
    parser.add_argument('--dry-run', action='store_true', help='Validate files and report without opening the catalogue')
    args = parser.parse_args(argv)
    if not args.dry_run and args.state_dir is None:
        parser.error('--state-dir is required when submitting')
    entries: dict[str, tuple[int, dict[str, Any]]] = {}
    scopes = set(args.scope)
    line_number = 0
    try:
        with args.manifest.open('rb') as stream:
            while line := stream.readline(MAX_LINE + 1):
                line_number += 1
                if line_number > MAX_ENTRIES or len(line) > MAX_LINE:
                    raise LibraryError('Telegram manifest exceeds its entry or line limit')
                oid, origin = _origin(json.loads(line), scopes)
                existing = entries.get(oid)
                if existing is not None:
                    _check_repeat(existing[1], origin)
                else:
                    entries[oid] = (line_number, origin)
        if not entries:
            raise LibraryError('Telegram manifest is empty')
        catalog = None if args.dry_run else Catalog(args.state_dir)
        for oid, (line_number, origin) in entries.items():
            status = 'would-submit' if catalog is None else _submit(catalog, oid, origin, at=datetime.now(timezone.utc))
            report = {key: origin['telegram'][key] for key in ('channel', 'message', 'topic', 'sha256')}
            if 'index_title' in origin['telegram']:
                report['index_title'] = origin['telegram']['index_title']
            print(json.dumps({**report, 'origin': oid, 'path': origin['path'], 'size': origin['size'], 'status': status}))
    except (LibraryError, ValueError, OSError, RecursionError) as exc:
        parser.exit(1, f'Telegram manifest line {line_number}: {exc}\n')


if __name__ == '__main__':
    main()
