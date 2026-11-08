"""Printed evidence stays separate from embedded metadata and manual decisions."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from library.metadata import isbn13, normalized
from library.catalog import now, LibraryError
from library.process import bounded_run


def printed_isbns(printed: dict[str, Any]) -> tuple[set[str], bool]:
    codes: set[str] = set()
    invalid = False
    for page in printed.get('pages', []):
        for line in page.get('text', '').splitlines():
            labelled = list(re.finditer(r'\bISBN(?:[- ]?13)?\s*[:：]?\s*([0-9][0-9\s\-\"\']+)', line, re.I))
            values = [match.group(1) for match in labelled] if labelled else [line.strip()]
            for raw in values:
                if not labelled and not re.fullmatch(r'[0-9\s\-\"\'.]+', raw):
                    continue
                digits = re.sub(r'[^0-9]', '', raw)
                if not labelled and len(digits) != 13:
                    continue
                code = isbn13(digits)
                if code:
                    codes.add(code)
                elif labelled or len(digits) == 13:
                    invalid = True
    return codes, invalid


def words(value: str) -> str:
    text = ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+', text))


def identify_printed(printed: dict[str, Any], embedded: dict[str, Any],
                     candidates: list[dict[str, Any]]) -> dict[str, Any]:
    pending: dict[str, Any] = {'status': 'pending', 'reason': 'Printed edition evidence is incomplete',
                               'candidates': candidates}
    codes, invalid = printed_isbns(printed)
    if printed.get('status') != 'ok' or invalid or len(codes) != 1:
        return {**pending, 'reason': 'Printed ISBN is missing, invalid or contradictory in the bounded window'}
    code = next(iter(codes))
    matches = [c for c in candidates if c.get('isbn') == code and c.get('source') == 'bnf']
    if len(matches) != 1:
        return {**pending, 'reason': 'Printed ISBN requires one exact BnF edition'}
    candidate = matches[0]
    required = ('title', 'author', 'number', 'language', 'publisher', 'date', 'work_ref', 'edition_ref', 'url')
    if candidate.get('medium') != 'print' or any(not candidate.get(k) for k in required):
        return pending
    front = printed['pages'][:4]
    text = ' ' + words('\n'.join(p['text'] for p in front)) + ' '
    authors = [candidate['author'], *candidate.get('author_forms', [])]
    # Qualified route: ISBN and author on the first image (full jacket),
    # then the album title and explicit volume in its first four images.
    # End advertisements and isolated matching strings cannot provide this anchor.
    first = ' ' + words(front[0]['text']) + ' ' if front else ''
    first_codes, first_invalid = printed_isbns({'pages': front[:1]})
    volumes = set(re.findall(r'\b(?:volume|vol|tome) (\d+)\b', text))
    if (first_codes != {code} or first_invalid or
            not any(' ' + words(author) + ' ' in first for author in authors) or
            volumes != {str(candidate['number'])}):
        return {**pending, 'reason': 'No consistent front-jacket ISBN/author and opening-volume anchor'}
    concordance = {
        'title': [p['name'] for p in front if ' ' + words(candidate['title']) + ' ' in ' ' + words(p['text']) + ' '],
        'author': [p['name'] for p in front if any(' ' + words(a) + ' ' in ' ' + words(p['text']) + ' ' for a in authors)],
        'number': [p['name'] for p in front if re.search(r'\b(?:volume|vol|tome) ' + re.escape(str(candidate['number'])) + r'\b', words(p['text']))]}
    if not all(concordance.values()):
        return {**pending, 'reason': 'Printed title, author or volume does not agree with exact edition'}
    # Unknown fields stay unknown. Present structured contradictions still block.
    for source_field, field in [('Title', 'title'), ('Writer', 'author'), ('Number', 'number'),
                                ('LanguageISO', 'language'), ('Publisher', 'publisher'),
                                ('EditionDate', 'date'), ('EditionRevision', 'revision')]:
        if embedded.get(source_field) and normalized(embedded[source_field]) != normalized(candidate.get(field, '')):
            return {**pending, 'reason': f'Embedded {field} contradicts printed edition'}
    embedded_code = embedded.get('ISBN', embedded.get('GTIN'))
    if embedded_code and isbn13(str(embedded_code)) != code:
        return {**pending, 'reason': 'Embedded ISBN contradicts printed edition'}
    if re.search(r'\b(?:revue|revisee|revised|augmentee|abridged)\b', text):
        return {**pending, 'reason': 'Printed revision requires manual review'}
    return {'status': 'identified', 'reason': 'Exact printed ISBN and consistent title, author and volume',
            'candidates': candidates, 'candidate': candidate,
            'evidence': {'isbn': code, 'url': candidate['url'], 'source': 'bnf',
                         'representation': 'scan_of_print_edition', 'anchor': 'first_image_isbn_author_and_opening_title_volume', 'printed': printed,
                         'fields': concordance, 'isbn_scope': 'all eight sampled pages; not the complete archive', 'retrieved_at': now()}}


def read_printed(path: Any, inspection: dict[str, Any]) -> dict[str, Any]:
    """Read eight validated archive pages in a separate networkless worker."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from library.catalog import LibraryError
    from library.readers import fingerprint, sha256
    path = Path(path).absolute()
    if (any(p.is_symlink() for p in (path, *path.parents)) or
            inspection.get('status') != 'validated' or inspection.get('format') not in ('cbz', 'cbr', 'pdf')):
        raise LibraryError('Printed evidence requires a validated comic input')
    before = fingerprint(path)
    expected = inspection['sha256']
    if sha256(path) != expected:
        raise LibraryError('Printed evidence source changed')
    manifest = inspection.get('page_manifest', [])
    selected = list({p['name']: p for p in [*manifest[:4], *manifest[-4:]]}.values())
    if inspection['format'] == 'pdf':
        count = inspection['pages']
        selected = [{'page': i} for i in sorted(set([*range(1, min(4, count) + 1), *range(max(1, count - 3), count + 1)]))]
    if not selected:
        raise LibraryError('Printed evidence requires a validated page manifest')
    from library.sandbox import native_prefix, PYTHON
    command = native_prefix(path, ocr=True) + [PYTHON, '-m', 'library.printed_worker',
                                             '/tmp/input', '.' + inspection['format']]
    image = os.environ.get('BOOKSYS_READER_IMAGE')
    container_name = None
    if image:
        import uuid
        container_name = 'booksys-ocr-' + uuid.uuid4().hex
        if not re.fullmatch(r'[a-z0-9./:_@-]+', image) or ',' in str(path) or '\n' in str(path):
            raise LibraryError('Invalid printed reader image/bind')
        command = ['docker', 'run', '--rm', '-i', '--name', container_name, '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', f'{os.getuid()}:{os.getgid()}', '--memory', '1536m', '--cpus', '1',
            '--pids-limit', '32', '--mount', f'type=bind,src={path},dst=/input,readonly',
            '--entrypoint', '/app/.venv/bin/python', image, '-m', 'library.printed_worker',
            '/input', '.' + inspection['format']]
    try:
        process = bounded_run(command, input=json.dumps({'pages': selected}).encode(), timeout=180)
    except BaseException as exc:
        if container_name:
            stop_owned_container(container_name)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise LibraryError('Printed reader exceeded wall time; owned engine stopped') from exc
        raise
    if fingerprint(path) != before or sha256(path) != expected:
        raise LibraryError('Printed evidence source changed during OCR')
    if process.returncode or len(process.stdout) > 1024**2:
        raise LibraryError('Printed reader failed or exceeded resource limits')
    result: dict[str, Any] = json.loads(process.stdout)
    if result.pop('input_sha256', None) != expected:
        raise LibraryError('Printed reader inspected another source')
    if result.get('status') == 'ok':
        if inspection['format'] == 'pdf':
            if [p.get('page') for p in result['pages']] != [p['page'] for p in selected]:
                raise LibraryError('Printed PDF page selection changed')
        elif [(p['name'], p['sha256']) for p in result['pages']] != [(p['name'], p['sha256']) for p in selected]:
            raise LibraryError('Printed reader page evidence changed')
    result['asset'] = expected
    return result


def scan_printed(store: Any, asset_id: str, reception: Any) -> dict[str, Any]:
    from library.catalog import LibraryError
    from pathlib import Path
    from library.origins import resolve_asset
    state = store.read()
    asset = state['assets'][asset_id]
    result = read_printed(resolve_asset(store, Path(reception), state, asset_id), asset)
    asset.setdefault('printed_attempts', []).append({k: v for k, v in result.items() if k != 'pages'})
    asset['printed_attempts'] = asset['printed_attempts'][-10:]
    if result['status'] == 'ok':
        asset['printed'] = result
    store.save(state, expected=state['revision'])
    return result


def stop_owned_container(name: str) -> None:
    """Stop only this controller invocation's named one-shot and verify exit."""
    if not re.fullmatch(r'booksys-ocr-[0-9a-f]{32}', name):
        raise LibraryError('Invalid owned OCR container name')
    stopped = bounded_run(['docker', 'stop', '--time', '1', name], timeout=15, stdout_limit=8192)
    if stopped.returncode and b'No such container' not in stopped.stderr:
        raise LibraryError(f'Cannot stop owned OCR engine {name}; preserve its evidence')
    state = bounded_run(['docker', 'inspect', '--format', '{{.State.Running}}', name],
                        timeout=10, stdout_limit=8192)
    if state.returncode == 0 and state.stdout.strip() == b'false':
        return
    if state.returncode != 0 and b'No such' in state.stderr:
        return  # --rm already removed this completed one-shot.
    raise LibraryError(f'Owned OCR engine {name} termination is not proved')
