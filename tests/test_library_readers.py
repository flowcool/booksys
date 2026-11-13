import io
import os
import struct
import zlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image
import pikepdf

from library.readers import inspect_file

UNRAR = Path(__file__).resolve().parents[1] / 'tmp/qualification/native/unrar-root/usr/bin/unrar-nonfree'
if UNRAR.exists():
    os.environ.setdefault('BOOKSYS_UNRAR', str(UNRAR))


def rar_bytes(name, content, flags=0):
    def header(kind, flags, body):
        raw = struct.pack('<BHH', kind, flags, 7 + len(body)) + body
        return struct.pack('<H', zlib.crc32(raw) & 0xffff) + raw
    main = header(0x73, 0, b'\x00' * 6)
    name = name.encode()
    body = struct.pack('<IIBIIBBHI', len(content), len(content), 3,
        zlib.crc32(content), 0, 20, 0x30, len(name), 0o100644) + name
    return b'Rar!\x1a\x07\x00' + main + header(0x74, 0x8000 | flags, body) + content + header(0x7b, 0, b'')


def image_bytes():
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(output, format='PNG')
    return output.getvalue()


class ReaderTests(unittest.TestCase):



    def test_symlinks_encryption_corruption_and_limits_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'album.cbz'
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                with archive.open('big.png', 'w') as member:
                    for _ in range(65):
                        member.write(b'0' * 1024**2)
            self.assertEqual(inspect_file(path, stable_seconds=.01)['status'], 'rejected')
            with zipfile.ZipFile(path, 'w') as archive:
                link = zipfile.ZipInfo('link.png')
                link.external_attr = 0o120777 << 16
                archive.writestr(link, 'elsewhere')
            self.assertEqual(inspect_file(path, stable_seconds=.01)['status'], 'rejected')
            symlink = root / 'link.cbz'
            symlink.symlink_to(path)
            self.assertEqual(inspect_file(symlink, stable_seconds=.01)['status'], 'rejected')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('001.png', image_bytes())
            raw = bytearray(path.read_bytes())
            raw[6] |= 1
            offset = raw.index(b'PK\x01\x02')
            raw[offset + 8] |= 1
            path.write_bytes(raw)
            self.assertEqual(inspect_file(path, stable_seconds=.01)['status'], 'rejected')
            pdf_path = root / 'encrypted.pdf'
            with pikepdf.new() as pdf:
                pdf.add_blank_page()
                pdf.save(pdf_path, encryption=pikepdf.Encryption(owner='owner', user='password'))
            self.assertEqual(inspect_file(pdf_path, stable_seconds=.01)['status'], 'rejected')
            path.write_bytes(b'PK\x03\x04broken')
            self.assertEqual(inspect_file(path, stable_seconds=.01)['status'], 'rejected')






