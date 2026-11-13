"""Synthetic tools/workers inside the existing networkless Docker reader boundary.

No nested bwrap or Docker socket is used. Host bwrap confinement has a separate
unittest boundary. Every native prerequisite is required, never skipped.
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


def worker(module: str, path: Path, *arguments: str, request: dict | None = None) -> dict:
    process = subprocess.run([sys.executable, '-m', 'library.' + module, str(path),
                              path.suffix, *arguments], input=json.dumps(request or {}).encode(),
                             capture_output=True, timeout=90, check=True)
    result = json.loads(process.stdout)
    assert result['input_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest(), result
    return result


versions = {}
for tool, arguments in [('unrar', ['-?']), ('pdftoppm', ['-v']),
                        ('tesseract', ['--version']), ('bwrap', ['--version'])]:
    assert shutil.which(tool), 'Required image prerequisite missing: ' + tool
    result = subprocess.run([tool, *arguments], capture_output=True, timeout=5, check=True)
    versions[tool] = (result.stdout + result.stderr).decode().strip().splitlines()[:3]
languages = subprocess.run(['tesseract', '--list-langs'], capture_output=True,
                           text=True, timeout=5, check=True).stdout.splitlines()
assert {'eng', 'fra'}.issubset(languages), languages
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
    # A vector page forces the Poppler path rather than JPEG extraction.
    with pikepdf.new() as document:
        page = document.add_blank_page(page_size=(600, 200))
        font_object = document.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font_object))
        page.Contents = document.make_stream(
            b'BT /F1 24 Tf 20 140 Td (SYNTHETIC LIBRARY) Tj 0 -40 Td (ARCHIVE 1234567890) Tj ET')
        document.save(pdf)
    inspections, transforms = {}, {}
    xml = base64.b64encode(b'<ComicInfo><Title>Synthetic</Title></ComicInfo>').decode()
    for source in (cbz, cbr, pdf):
        before = source.read_bytes()
        suffix = source.suffix
        inspection = worker('reader_worker', source)
        assert inspection['status'] == 'validated' and inspection['pages'] == 1, inspection
        inspections[suffix] = inspection
        output = root / (suffix[1:] + '-output.cbz')
        transform = worker('reader_worker', source, '--transform', str(output), request={'comicinfo': xml})
        assert transform['status'] == 'validated', transform
        assert transform['artifact_sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
        readback = worker('reader_worker', output)
        assert readback['status'] == 'validated' and readback['pages'] == 1, readback
        assert readback['page_manifest'] == transform['page_manifest'], (readback, transform)
        if suffix == '.pdf':
            assert transform['page_methods'][0]['method'] == 'rendered_page', transform
            assert 'poppler' in transform['tool_versions'], transform
        if suffix == '.cbr':
            assert transform['tool_versions']['unrar'], transform
        assert source.read_bytes() == before, 'Original synthetic input changed'
        transforms[suffix] = transform
    # Malformed content must fail closed through the actual image reader.
    broken = root / 'broken.cbr'
    broken.write_bytes(cbr.read_bytes().replace(png, b'x' * len(png)))
    assert worker('reader_worker', broken)['status'] == 'rejected'
    active = root / 'active.pdf'
    with pikepdf.open(pdf) as document:
        document.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS='alert(1)')
        document.save(active)
    assert worker('reader_worker', active)['status'] == 'rejected'
    print(json.dumps({'boundary': 'Docker reader; no nested namespaces', 'native_versions': versions,
                      'languages': languages, 'inspections': inspections, 'transforms': transforms,
                      'confinement': 'uid1001; no capabilities; no new privileges; no network route; readonly root; no installation mounts',
                      'negative_cases': ['corrupt RAR', 'active PDF']}, indent=2))
