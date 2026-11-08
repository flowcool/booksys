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
    def test_bounded_exact_ancillary_entry_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'album.cbz'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('001.png', image_bytes())
                archive.writestr('Thumbs.db', b'opaque inert data')
            result = inspect_file(path, stable_seconds=.01)
            self.assertEqual(result['status'], 'validated', result)
            self.assertEqual(result['pages'], 1)
            self.assertEqual(result['ignored_ancillary'], ['Thumbs.db'])
            with zipfile.ZipFile(path, 'a') as archive:
                archive.writestr('other.db', b'not authorized')
            self.assertEqual(inspect_file(path, stable_seconds=.01)['status'], 'rejected')
    def test_scanned_pdf_is_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scan.pdf'
            Image.new('RGB', (40, 60), 'white').save(path, format='PDF')
            result = inspect_file(path, stable_seconds=.01)
            self.assertEqual(result['status'], 'validated', result)

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

    def test_rar_validation_and_multipart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'album.cbr'
            path.write_bytes(rar_bytes('001.png', image_bytes()))
            result = inspect_file(path, stable_seconds=0.01)
            self.assertEqual(result['status'], 'validated', result)
            path.write_bytes(rar_bytes('001.png', image_bytes(), flags=2))
            self.assertEqual(inspect_file(path, stable_seconds=0.01)['status'], 'pending')

    def test_archive_rejections_and_nested_groups(self):
        entries = [('nested.zip', b'PK', 'pending'), ('../page.png', image_bytes(), 'rejected'), ('bad.png', b'bad', 'rejected')]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'album.cbz'
            for name, data, expected in entries:
                with self.subTest(name=name):
                    with zipfile.ZipFile(path, 'w') as archive:
                        archive.writestr(name, data)
                    self.assertEqual(inspect_file(path, stable_seconds=0.01)['status'], expected)
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('volume1/001.png', image_bytes())
                archive.writestr('volume2/001.png', image_bytes())
            self.assertEqual(inspect_file(path, stable_seconds=0.01)['status'], 'pending')

    def test_valid_cbz_and_renamed_zip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'album.cbz'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('001.png', image_bytes())
                archive.writestr('ComicInfo.xml', '<ComicInfo><Title>Album</Title><Number>1</Number></ComicInfo>')
            result = inspect_file(path, stable_seconds=0.01)
            self.assertEqual(result['status'], 'validated')
            self.assertEqual(result['embedded']['Title'], 'Album')
            path.rename(path.with_suffix('.zip'))
            self.assertEqual(inspect_file(path.with_suffix('.zip'), stable_seconds=0.01)['status'], 'pending')

    def test_pdf_active_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'album.pdf'
            with pikepdf.new() as pdf:
                pdf.add_blank_page()
                pdf.save(path)
            self.assertEqual(inspect_file(path, stable_seconds=0.01)['status'], 'validated')
            with pikepdf.open(path) as pdf:
                pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS='alert(1)')
                pdf.save(path.with_name('active.pdf'))
            self.assertEqual(inspect_file(path.with_name('active.pdf'), stable_seconds=0.01)['status'], 'rejected')
            with pikepdf.open(path) as pdf:
                pdf.Root.TestLink = pikepdf.Dictionary(A=pikepdf.Dictionary(S=pikepdf.Name.GoTo, D='local-page'))
                pdf.save(path.with_name('internal.pdf'))
            self.assertEqual(inspect_file(path.with_name('internal.pdf'), stable_seconds=.01)['status'], 'validated')
