"""Qualify the current application roles without a database or external network."""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib

import pikepdf
from PIL import Image
from library import decode

from booksys_core.parsing import catalogue, source_plan
import psycopg
from psycopg import pq

assert sys.version_info[:2] == (3, 14)
assert psycopg.__version__ == "3.3.8"
assert pq.__impl__ == "c"
assert os.getuid() == 1000
assert Path("/usr/local/bin/docker").is_file()
for forbidden in ("/var/run/docker.sock", "/secrets", "/data/booksys", "/root/.ssh"):
    assert not Path(forbidden).exists(), forbidden

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


for tool in ('unrar', 'pdfimages', 'pdftoppm'):
    assert shutil.which(tool), 'Required image prerequisite missing: ' + tool
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    decode.WORK_ROOT = str(root)
    buffer = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(buffer, format='PNG')
    cbr, pdf = root / 'synthetic.cbr', root / 'synthetic.pdf'
    cbr.write_bytes(rar_bytes('001.png', buffer.getvalue()))
    # A vector page exercises both pdfimages and the pdftoppm fallback.
    with pikepdf.new() as document:
        page = document.add_blank_page(page_size=(600, 200))
        font = document.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.Contents = document.make_stream(b'BT /F1 24 Tf 20 140 Td (SYNTHETIC LIBRARY) Tj ET')
        document.save(pdf)
    for source in (cbr, pdf):
        outcome = decode.run('validate', source)
        assert outcome['status'] == 'validated' and outcome['pages'] == 1, outcome
    output = root / 'output'
    output.mkdir()
    payload = {'comicinfo': base64.b64encode(b'<ComicInfo><Title>Synthetic</Title></ComicInfo>').decode()}
    outcome = decode.run('transform', pdf, payload, destination=output)
    assert outcome['status'] == 'validated' and outcome['pages'] == 1, outcome
    with zipfile.ZipFile(output / 'book.cbz') as archive:
        with Image.open(io.BytesIO(archive.read(outcome['page_manifest'][0]['name']))) as page_image:
            assert page_image.format == 'JPEG' and page_image.size == (1250, 417)

roles = ("library.cli", "library.stage_worker", "ops.library_service")
for role in roles:
    result = subprocess.run([sys.executable, "-m", role, "--help"],
        capture_output=True, text=True, timeout=20, check=True)
    assert "usage:" in result.stdout.lower(), role
category = "https://www.bookys-ebooks.com/bandes-dessinees/new"
source = "https://www.bookys-ebooks.com/bandes-dessinees/1-synthetic"
items, following = catalogue('<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="' + source + '">Synthetic</a></div>', category)
assert items == {source: "Synthetic"} and following is None
plan = source_plan('<h1>Synthetic</h1><a href="/bandes-dessinees/1-synthetic/dl/1">fixture</a>', source, None)
assert plan
namespace = {"__name__": "image_provider_spacing"}
exec(compile(sys.stdin.read(), "test_provider_spacing.py", "exec"), namespace)
suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace["ProviderSpacingTests"])
assert suite.countTestCases() == 2
result = unittest.TextTestRunner(verbosity=2).run(suite)
assert result.wasSuccessful() and result.testsRun == 2 and not result.skipped
print(json.dumps({"probe": "worker", "roles": roles, "python": list(sys.version_info[:2]),
                  "postgresql_driver": psycopg.__version__, "synthetic_parser": True, "rar_pdf_decode": True,
                  "provider_business_tests": result.testsRun,
                  "database_connection": False, "external_network": False}))
