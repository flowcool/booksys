"""Synthetic tools/workers inside the existing networkless Docker reader boundary.

No nested namespace or Docker socket is used. Every native tool used by the
reader is required, never skipped.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

import pikepdf
from PIL import Image, ImageDraw, ImageFont


def rar_bytes(name: str, content: bytes) -> bytes:
    # Minimal stored RAR4, generated from synthetic bytes (no captured archive).
    def header(kind: int, flags: int, body: bytes) -> bytes:
        raw = struct.pack('<BHH', kind, flags, 7 + len(body)) + body
        return struct.pack('<H', zlib.crc32(raw) & 0xffff) + raw
    encoded = name.encode()
    body = struct.pack('<IIBIIBBHI', len(content), len(content), 3,
                       zlib.crc32(content), 0, 20, 0x30, len(encoded), 0o100644) + encoded
    return (b'Rar!\x1a\x07\x00' + header(0x73, 0, b'\x00' * 6)
            + header(0x74, 0x8000, body) + content + header(0x7b, 0, b''))


def worker(path: Path, operation: str, workdir: Path, *, request: dict | None = None) -> dict:
    workdir.mkdir()
    process = subprocess.run([sys.executable, '-m', 'library.reader_worker', str(path),
                              path.suffix, operation, str(workdir)], input=json.dumps(request or {}).encode(),
                             capture_output=True, timeout=90, check=True)
    result = json.loads(process.stdout)
    assert result['input_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest(), result
    return result


versions = {}
for tool, arguments in [('unrar', ['-?']), ('pdfimages', ['-v']), ('pdftoppm', ['-v'])]:
    assert shutil.which(tool), 'Required image prerequisite missing: ' + tool
    result = subprocess.run([tool, *arguments], capture_output=True, timeout=5, check=True)
    versions[tool] = (result.stdout + result.stderr).decode().strip().splitlines()[:3]
assert os.getuid() == 1001
status = Path('/proc/self/status').read_text()
assert 'CapEff:\t0000000000000000' in status and 'NoNewPrivs:\t1' in status
assert len(Path('/proc/net/route').read_text().splitlines()) == 1, 'Unexpected network route'
for forbidden in ('/var/run/docker.sock', '/input', '/output', '/data/booksys', '/root/.ssh'):
    assert not Path(forbidden).exists(), forbidden
try:
    Path('/app/unexpected-probe-write').write_text('must fail')
except OSError:
    pass
else:
    raise AssertionError('Image root is writable')

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    image = Image.new('RGB', (1200, 350), 'white')
    font = ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf', 48)
    ImageDraw.Draw(image).multiline_text((30, 30),
        'SYNTHETIC LIBRARY\nARCHIVE 1234567890\nREADER VALIDATION', font=font, fill='black', spacing=15)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    png = buffer.getvalue()
    cbz, cbr, pdf = (root / ('synthetic' + suffix) for suffix in ('.cbz', '.cbr', '.pdf'))
    with zipfile.ZipFile(cbz, 'w') as archive:
        archive.writestr('001.png', png)
    cbr.write_bytes(rar_bytes('001.png', png))
    # A vector page forces the 150 dpi JPEG fallback rather than image extraction.
    with pikepdf.new() as document:
        page = document.add_blank_page(page_size=(600, 200))
        font_object = document.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font_object))
        page.Contents = document.make_stream(
            b'BT /F1 24 Tf 20 140 Td (SYNTHETIC LIBRARY) Tj 0 -40 Td (ARCHIVE 1234567890) Tj ET')
        document.save(pdf)
    embedded_pdf = root / 'embedded.pdf'
    image.save(embedded_pdf, format='PDF')
    with pikepdf.open(embedded_pdf) as document:
        embedded_jpeg = next(iter(document.pages[0].Resources.XObject.values())).read_raw_bytes()
    inspections, transforms = {}, {}
    xml = base64.b64encode(b'<ComicInfo><Title>Synthetic</Title></ComicInfo>').decode()
    for case, source in {'.cbz': cbz, '.cbr': cbr, '.pdf': pdf, 'embedded.pdf': embedded_pdf}.items():
        before = source.read_bytes()
        suffix = source.suffix
        inspection = worker(source, 'validate', root / (case + '-validate'))
        assert inspection['status'] == 'validated' and inspection['pages'] == 1, inspection
        inspections[case] = inspection
        workdir = root / (case + '-transform')
        output = workdir / 'book.cbz'
        transform = worker(source, 'transform', workdir, request={'comicinfo': xml})
        assert transform['status'] == 'validated', transform
        assert transform['artifact_sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
        with zipfile.ZipFile(output) as archive:
            assert archive.read('ComicInfo.xml') == base64.b64decode(xml)
            if suffix == '.pdf':
                page_bytes = archive.read(transform['page_manifest'][0]['name'])
                if source == embedded_pdf:
                    assert page_bytes == embedded_jpeg, 'Embedded JPEG was not preserved'
                else:
                    with Image.open(io.BytesIO(page_bytes)) as page_image:
                        assert page_image.format == 'JPEG' and page_image.size == (1250, 417)
        readback = worker(output, 'validate', root / (case + '-readback'))
        assert readback['status'] == 'validated' and readback['pages'] == 1, readback
        assert readback['page_manifest'] == transform['page_manifest'], (readback, transform)
        assert readback['embedded']['Title'] == 'Synthetic', readback
        if suffix == '.pdf':
            assert transform['tool_versions']['pikepdf'] == pikepdf.__version__, transform
        if suffix == '.cbr':
            assert transform['tool_versions']['unrar'], transform
        assert source.read_bytes() == before, 'Original synthetic input changed'
        transforms[case] = transform
    # Malformed content must fail closed through the actual image reader.
    broken = root / 'broken.cbr'
    broken.write_bytes(cbr.read_bytes().replace(png, b'x' * len(png)))
    assert worker(broken, 'validate', root / 'broken-validate')['status'] == 'rejected'
    for source, case in ((pdf, '.pdf'), (embedded_pdf, 'embedded.pdf')):
        active = root / ('active-' + source.name)
        with pikepdf.open(source) as document:
            document.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS='alert(1)')
            document.save(active)
        # Validation only counts pages with pikepdf. Poppler extracts images or
        # rasterizes page content; neither conversion path executes OpenAction.
        # The CBZ retains only page images and ComicInfo, never PDF actions.
        active_result = worker(active, 'validate', root / (case + '-active-validate'))
        assert active_result['status'] == 'validated' and active_result['pages'] == 1, active_result
        workdir = root / (case + '-active-transform')
        active_transform = worker(active, 'transform', workdir, request={'comicinfo': xml})
        assert active_transform['status'] == 'validated', active_transform
        assert active_transform['page_manifest'] == transforms[case]['page_manifest'], active_transform
        with zipfile.ZipFile(workdir / 'book.cbz') as archive:
            assert set(archive.namelist()) == {active_transform['page_manifest'][0]['name'], 'ComicInfo.xml'}
    print(json.dumps({'boundary': 'Docker reader; no nested namespaces', 'native_versions': versions,
                      'inspections': inspections, 'transforms': transforms,
                      'confinement': 'uid1001; no capabilities; no new privileges; no network route; readonly root; no installation mounts',
                      'negative_cases': ['corrupt RAR'],
                      'inert_actions': 'active PDFs validate and yield the same page images; actions are omitted'}, indent=2))
