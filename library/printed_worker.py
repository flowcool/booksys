"""Internal OCR worker."""
from __future__ import annotations

import hashlib
import json
import os
import resource
import subprocess
import sys
from pathlib import Path
from typing import Any

from library.primitives import LibraryError
from library.reader_worker import inspect_archive
from library.readers import sha256


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (150, 150))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    path, suffix = Path(sys.argv[1]), sys.argv[2]
    output: dict[str, Any]
    try:
        request = json.loads(sys.stdin.buffer.read(16385))
        selected = request['pages']
        if not isinstance(selected, list) or not 1 <= len(selected) <= 8:
            raise LibraryError('OCR page count limit')
        images: dict[str, bytes] = {}
        if suffix == '.pdf':
            from library.pdf_pages import convert_pdf_pages
            indices = [p['page'] for p in selected]
            from library.pdf_pages import inspect_pdf
            count = inspect_pdf(path)['pages']
            actual_indices = sorted(set([*range(1, min(4, count) + 1), *range(max(1, count - 3), count + 1)]))
            if indices != actual_indices:
                raise LibraryError('OCR PDF window differs from validated page count')
            inspection = convert_pdf_pages(path, lambda name, data: images.update({name: data}), indices=indices)
            selected = [{**entry, 'page': index} for entry, index in zip(inspection['page_manifest'], indices)]
        else:
            wanted = {page['name']: page for page in selected}
            def collect(name: str, data: bytes) -> None:
                if name in wanted:
                    if hashlib.sha256(data).hexdigest() != wanted[name]['sha256']:
                        raise LibraryError('OCR page hash mismatch')
                    images[name] = data
            inspection = inspect_archive(path, suffix, sink=collect)
            if inspection['status'] != 'validated':
                raise LibraryError('OCR archive is not validated')
            manifest = inspection['page_manifest']
            actual = list({p['name']: p for p in [*manifest[:4], *manifest[-4:]]}.values())
            if actual != selected or set(images) != set(wanted):
                raise LibraryError('OCR window differs from validated manifest')
        tool = os.environ.get('BOOKSYS_TESSERACT', '/usr/bin/tesseract')
        version = subprocess.run([tool, '--version'], timeout=5, capture_output=True, check=False)
        if version.returncode:
            raise LibraryError('OCR version probe failed')
        pages: list[dict[str, Any]] = []
        for page in selected:
            result = subprocess.run([tool, 'stdin', 'stdout', '-l', 'eng+fra', '--psm', '3'],
                input=images[page['name']], timeout=30, capture_output=True, check=False,
                env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
            if result.returncode:
                raise LibraryError('OCR engine failed')
            if len(result.stdout) > 65536:
                raise LibraryError('OCR text output limit')
            pages.append({**page, 'text': result.stdout.decode('utf-8')})
        output = {'status': 'ok', 'pages': pages, 'tool': version.stdout.decode().splitlines()[0],
                  'window': 'first4_last4', 'languages': ['eng', 'fra'], 'psm': 3}
    except Exception as exc:
        output = {'status': 'pending', 'reason': f'{type(exc).__name__}: {exc}'}
    output['input_sha256'] = sha256(path)
    print(json.dumps(output))


if __name__ == '__main__':
    main()
