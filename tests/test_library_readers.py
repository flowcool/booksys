import io
import os
import shutil
import struct
import zlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

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

    def test_generic_zip_uses_the_comic_reader_and_publishes_cbz(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'volume.ZIP'
            with zipfile.ZipFile(source, 'w') as archive:
                for name in ('10.png', '2.png'):
                    archive.writestr(name, image_bytes())
                archive.writestr('ComicInfo.xml', '<ComicInfo><Title>Volume</Title></ComicInfo>')
                for name in ('note.txt', 'release.nfo', 'thumbs.db', '.picasa.ini'):
                    archive.writestr(name, 'ancillary')
            manifest = self.check_generic_archive(root, source, '.cbz')
            self.assertEqual([page['name'] for page in manifest], ['2.png', '10.png'])

    @unittest.skipUnless(shutil.which('unrar'), 'native UnRAR required')
    def test_generic_rar_uses_the_comic_reader_and_publishes_cbz(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'volume.rar'
            source.write_bytes(rar_bytes('T01/1.png', image_bytes()))
            manifest = self.check_generic_archive(root, source, '.cbr')
            self.assertEqual([page['name'] for page in manifest], ['T01/1.png'])

    def check_generic_archive(self, root, source, comic_suffix):
        from library.decode import run
        from library.transform import transform_archive

        original = source.read_bytes()
        comic = source.with_suffix(comic_suffix)
        comic.write_bytes(original)
        work = root / 'work'
        work.mkdir()
        with patch('library.decode.WORK_ROOT', str(work)):
            result = inspect_file(source, stable_seconds=0)
            self.assertEqual(result['status'], 'validated', result)
            self.assertEqual(result, inspect_file(comic, stable_seconds=0))
            manifest = result['page_manifest']
            cover = root / 'cover'
            cover.mkdir()
            first_page = run('first_page', source, {'asset_sha256': result['sha256'],
                'reader_policy': result['reader_policy'], 'member': manifest[0]}, destination=cover)
            self.assertEqual(first_page['status'], 'extracted', first_page)
            self.assertEqual(first_page['member'], manifest[0])
            output = root / 'copy'
            output.mkdir()
            transform_archive(source, output, b'<ComicInfo/>', result['sha256'])
            self.assertEqual(inspect_file(output / 'book.cbz', stable_seconds=0)['page_manifest'], manifest)
        self.assertEqual(source.read_bytes(), original)
        return manifest

    def test_generic_multi_volume_and_mismatched_suffix_stay_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page = image_bytes()
            source = root / 'volumes.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('Series/T01/01.png', page)
                archive.writestr('Series/T02/01.png', page)
                for number in range(2, 2002):
                    archive.writestr(f'Series/T02/{number:04}.png', page)
            work = root / 'work'
            work.mkdir()
            with patch('library.decode.WORK_ROOT', str(work)):
                result = inspect_file(source, stable_seconds=0)
                self.assertEqual(result['status'], 'pending', result)
                self.assertIn('possible multi-work archive', result['reason'])
                with zipfile.ZipFile(source, 'w') as archive:
                    archive.writestr('01.png', page)
                for suffix in ('.rar', '.cbr'):
                    wrong_suffix = source.with_suffix(suffix)
                    wrong_suffix.write_bytes(source.read_bytes())
                    result = inspect_file(wrong_suffix, stable_seconds=0)
                    self.assertEqual(result['status'], 'pending', result)
                    self.assertEqual(result['reason'], 'Suffix and actual format differ')



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






