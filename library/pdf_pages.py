"""Bounded PDF page images: proven simple extraction, otherwise Poppler rendering."""
from __future__ import annotations

import hashlib
import io
import math
import os
from pathlib import Path
from typing import Any, Callable

import pikepdf
from PIL import Image

from library.catalog import LibraryError
from library.process import bounded_run

DPI = 200
MAX_PIXELS = 50_000_000
MAX_PAGE_BYTES = 64 * 1024**2


def rotation(page: Any) -> int:
    node = page.obj
    for _ in range(32):
        if '/Rotate' in node:
            return int(node['/Rotate']) % 360
        node = node.get('/Parent')
        if node is None:
            return 0
    raise LibraryError('PDF page inheritance limit')


def geometry(page: Any) -> dict[str, Any]:
    crop = list(map(float, page.cropbox))
    unit = float(page.obj.get('/UserUnit', 1))
    width, height = (crop[2] - crop[0]) * unit, (crop[3] - crop[1]) * unit
    angle = rotation(page)
    if not all(math.isfinite(n) and n > 0 for n in (width, height, unit)) or angle not in (0, 90, 180, 270):
        raise LibraryError('Invalid PDF page geometry')
    if angle in (90, 270):
        width, height = height, width
    pixels = math.ceil(width * DPI / 72), math.ceil(height * DPI / 72)
    if pixels[0] * pixels[1] > MAX_PIXELS:
        raise LibraryError('PDF rendered page pixel limit')
    return {'cropbox': crop, 'rotation': angle, 'user_unit': unit, 'render_size': pixels}


def extract_page(pdf: Any, page: Any, shape: dict[str, Any]) -> bytes | None:
    """Accept only one uncomplicated full-page DeviceRGB/Gray JPEG."""
    box = shape['cropbox']
    if (shape['rotation'] or shape['user_unit'] != 1 or box[:2] != [0, 0]
            or list(map(float, page.mediabox)) != box or pdf.Root.get('/OutputIntents')
            or pdf.Root.get('/OCProperties')
            or page.obj.get('/Annots') or page.obj.get('/Group')
            or page.Resources.get('/ColorSpace', {}).get('/DefaultRGB')
            or page.Resources.get('/ColorSpace', {}).get('/DefaultGray')):
        return None
    instructions = list(pikepdf.parse_content_stream(page))
    operators = [str(item.operator) for item in instructions]
    if operators[:1] == ['q'] and operators[-1:] == ['Q']:
        instructions = instructions[1:-1]
        operators = operators[1:-1]
    if not 2 <= len(instructions) <= 5 or operators[-1] != 'Do' or any(op != 'cm' for op in operators[:-1]):
        return None
    effective = pikepdf.Matrix()
    for instruction in instructions[:-1]:
        if len(instruction.operands) != 6:
            return None
        effective = pikepdf.Matrix(*map(float, instruction.operands)) @ effective
    matrix = list(effective.shorthand)
    expected = [box[2], 0, 0, box[3], 0, 0]
    if len(matrix) != 6 or any(not math.isclose(a, b, abs_tol=1e-7) for a, b in zip(matrix, expected)):
        return None
    images = page.images
    name = str(instructions[-1].operands[0])
    if len(images) != 1 or name not in images:
        return None
    image = images[name]
    if (str(image.get('/Filter')) != '/DCTDecode'
            or str(image.get('/ColorSpace')) not in ('/DeviceRGB', '/DeviceGray')
            or image.get('/BitsPerComponent') != 8
            or any(image.get(key) for key in ('/SMask', '/Mask', '/Decode', '/DecodeParms', '/ImageMask', '/Intent', '/Interpolate', '/OC'))):
        return None
    width, height = int(image['/Width']), int(image['/Height'])
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise LibraryError('PDF extracted image pixel limit')
    if not math.isclose(width / height, box[2] / box[3], rel_tol=1e-7):
        return None
    buffer = io.BytesIO()
    extension = pikepdf.PdfImage(image).extract_to(stream=buffer)
    data = buffer.getvalue()
    if extension != '.jpg' or len(data) > MAX_PAGE_BYTES or data != image.read_raw_bytes():
        return None
    with Image.open(io.BytesIO(data)) as decoded:
        if (decoded.size != (width, height) or decoded.mode not in ('RGB', 'L')
                or decoded.info.get('icc_profile') or decoded.getexif().get(274, 1) != 1):
            return None
        decoded.load()
    return data


def convert_pdf_pages(path: Path, sink: Callable[[str, bytes], None],
                      *, indices: list[int] | None = None) -> dict[str, Any]:
    from library.reader_worker import inspect_pdf, MAX_EXPANDED
    inspection = inspect_pdf(path)
    wanted = indices if indices is not None else list(range(1, inspection['pages'] + 1))
    if not wanted or wanted != sorted(set(wanted)) or any(type(i) is not int or not 1 <= i <= inspection['pages'] for i in wanted):
        raise LibraryError('Invalid PDF page selection')
    manifest, methods = [], []
    expanded = 0
    tools: dict[str, Any] = {'pikepdf': pikepdf.__version__}
    with pikepdf.open(path, attempt_recovery=False) as pdf:
        for index in wanted:
            page = pdf.pages[index - 1]
            shape = geometry(page)
            data = extract_page(pdf, page, shape)
            method, extension, dpi = 'extracted_image', 'jpg', None
            if data is None:
                tool = os.environ.get('BOOKSYS_PDFTOPPM', '/usr/bin/pdftoppm')
                if 'poppler' not in tools:
                    version = bounded_run([tool, '-v'], timeout=5, stdout_limit=8192, stderr_limit=8192)
                    if version.returncode:
                        raise LibraryError('PDF renderer version probe failed')
                    tools['poppler'] = (version.stdout + version.stderr).decode(errors='replace').splitlines()[0]
                rendered = bounded_run([tool, '-f', str(index), '-l', str(index), '-singlefile',
                    '-r', str(DPI), '-cropbox', '-png', str(path)], timeout=30, stdout_limit=MAX_PAGE_BYTES)
                if rendered.returncode:
                    raise LibraryError('PDF page renderer failed')
                data = rendered.stdout
                method, extension, dpi = 'rendered_page', 'png', DPI
            with Image.open(io.BytesIO(data)) as decoded:
                width, height = decoded.size
                if width * height > MAX_PIXELS:
                    raise LibraryError('PDF output pixel limit')
                if method == 'rendered_page' and any(abs(a - b) > 1 for a, b in zip(decoded.size, shape['render_size'])):
                    raise LibraryError('PDF rendered geometry mismatch')
                decoded.load()
            expanded += len(data)
            if len(data) > MAX_PAGE_BYTES or expanded > MAX_EXPANDED:
                raise LibraryError('PDF converted page size limit')
            name = f'{index:06d}.{extension}'
            sink(name, data)
            manifest.append({'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
            methods.append({'page': index, 'method': method, 'dpi': dpi, 'width': width, 'height': height,
                            'cropbox': shape['cropbox'], 'rotation': shape['rotation'], 'user_unit': shape['user_unit']})
    return {'status': 'validated', 'format': 'pdf', 'pages': len(wanted), 'page_manifest': manifest,
            'page_methods': methods, 'ancillary_manifest': {}, 'tool_versions': tools}
