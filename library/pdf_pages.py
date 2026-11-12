"""PDF pages extracted by Poppler, with JPEG rendering as a fallback."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable

import pikepdf

from library.primitives import LibraryError

MAX_EXPANDED = 2 * 1024**3
MAX_PAGES = 2000


def inspect_pdf(path: Path) -> dict[str, Any]:
    with pikepdf.open(path) as pdf:
        count = len(pdf.pages)
    if not 0 < count <= MAX_PAGES:
        raise LibraryError('Empty PDF or page count limit')
    return {'status': 'validated', 'format': 'pdf', 'pages': count, 'embedded': {}}


def convert_pdf_pages(path: Path, sink: Callable[[str, bytes], None],
                      *, indices: list[int] | None = None) -> dict[str, Any]:
    count = inspect_pdf(path)['pages']
    wanted = indices if indices is not None else list(range(1, count + 1))
    if not wanted or wanted != sorted(set(wanted)) or any(type(i) is not int or not 1 <= i <= count for i in wanted):
        raise LibraryError('Invalid PDF page selection')
    manifest = []
    expanded = 0
    with tempfile.TemporaryDirectory() as directory:
        prefix = Path(directory) / 'img'
        subprocess.run(['pdfimages', '-all', str(path), str(prefix)],
                       timeout=300, capture_output=True, check=True)
        images = sorted(Path(directory).iterdir())
        extracted = len(images) == count and all(p.suffix in ('.jpg', '.png', '.jp2', '.tif') for p in images)
        for index in wanted:
            if extracted:
                image = images[index - 1]
                data, extension = image.read_bytes(), image.suffix
            else:
                rendered = subprocess.run(['pdftoppm', '-r', '150', '-jpeg', '-jpegopt', 'quality=90',
                    '-f', str(index), '-l', str(index), '-singlefile', str(path)],
                    timeout=300, capture_output=True, check=True)
                data, extension = rendered.stdout, '.jpg'
            expanded += len(data)
            if expanded > MAX_EXPANDED:
                raise LibraryError('PDF converted output size limit')
            name = f'{index:06d}{extension}'
            sink(name, data)
            manifest.append({'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
    return {'status': 'validated', 'format': 'pdf', 'pages': len(wanted), 'page_manifest': manifest,
            'ancillary_manifest': {}, 'tool_versions': {'pikepdf': pikepdf.__version__}}
