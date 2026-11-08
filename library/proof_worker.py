"""Read one validated source page inside the existing networkless sandbox."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import resource
import sys
from pathlib import Path
from PIL import Image, ImageOps
from library.catalog import LibraryError
from library.readers import sha256


def main() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024**2, 1536 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    path, suffix, index = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    images: dict[str, bytes] = {}
    if suffix == '.pdf':
        from library.pdf_pages import convert_pdf_pages
        result = convert_pdf_pages(path, lambda name, data: images.update({name: data}), indices=[index])
        page = result['page_manifest'][0]
    else:
        from library.reader_worker import inspect_archive
        result = inspect_archive(path, suffix)
        if result['status'] != 'validated' or not 1 <= index <= len(result['page_manifest']):
            raise LibraryError('Invalid proof page')
        page = result['page_manifest'][index - 1]
        inspect_archive(path, suffix, sink=lambda name, data: images.update({name: data}) if name == page['name'] else None)
    data = images[page['name']]
    if hashlib.sha256(data).hexdigest() != page['sha256']:
        raise LibraryError('Proof page changed')
    with Image.open(io.BytesIO(data)) as raw:
        image = ImageOps.exif_transpose(raw).convert('RGB')
        # Archive pages have no trustworthy physical DPI. Never upscale them.
        # PDF pages are rendered/extracted by the qualified 200-dpi converter.
        image.thumbnail((2400, 3200))
        while True:
            out = io.BytesIO()
            image.save(out, format='PNG')
            png = out.getvalue()
            if len(png) <= 2 * 1024**2:
                break
            if min(image.size) < 300:
                raise LibraryError('Proof page cannot fit readable bounds')
            image.thumbnail((int(image.width * .8), int(image.height * .8)))
        print(json.dumps({'asset': sha256(path), 'page': index, 'name': page['name'],
            'source_sha256': page['sha256'], 'width': image.width, 'height': image.height,
            'dpi_max': 200, 'png': base64.b64encode(png).decode()}))


if __name__ == '__main__':
    main()
