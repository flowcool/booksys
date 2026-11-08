import hashlib
import io
import tempfile
import unittest
from pathlib import Path

import pikepdf
from PIL import Image

from library.pdf_pages import convert_pdf_pages
from library.catalog import LibraryError
from library.readers import inspect_file
from library.transform import transform_archive
import zipfile


def scan_pdf(path, *, rotation=0, crop=False, colour='RGB', overlay=False, double_matrix=False, default_colour=False, interpolate=False, optional_content=False):
    buffer = io.BytesIO()
    Image.new(colour, (120, 180), 'red' if colour == 'RGB' else (0, 255, 255, 0)).save(buffer, format='JPEG')
    image_bytes = buffer.getvalue()
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(120, 180))
        image = pdf.make_stream(image_bytes)
        image.Type = pikepdf.Name.XObject
        image.Subtype = pikepdf.Name.Image
        image.Width, image.Height, image.BitsPerComponent = 120, 180, 8
        image.ColorSpace = pikepdf.Name.DeviceRGB if colour == 'RGB' else pikepdf.Name.DeviceCMYK
        image.Filter = pikepdf.Name.DCTDecode
        if optional_content:
            group = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name='Hidden'))
            image.OC = group
            pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=pikepdf.Array([group]),
                D=pikepdf.Dictionary(BaseState=pikepdf.Name.OFF, OFF=pikepdf.Array([group])))
        if interpolate:
            image.Interpolate = True
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image))
        if default_colour:
            page.Resources.ColorSpace = pikepdf.Dictionary(DefaultRGB=pikepdf.Array([
                pikepdf.Name.CalRGB, pikepdf.Dictionary(WhitePoint=pikepdf.Array([.9505, 1, 1.089]),
                Gamma=pikepdf.Array([1.8, 1.8, 1.8]))]))
        content = b'120 0 0 180 0 0 cm /Im0 Do'
        if double_matrix:
            content = b'1 0 0 -1 0 180 cm 120 0 0 -180 0 180 cm /Im0 Do'
        if overlay:
            content += b' 0 0 1 rg 0 0 0.25 0.25 re f'
        page.Contents = pdf.make_stream(content)
        page.Rotate = rotation
        if crop:
            page.CropBox = pikepdf.Array([10, 20, 90, 140])
        if default_colour:
            page.obj.Parent.Resources = page.Resources
            del page.obj['/Resources']
        pdf.save(path)
    return image_bytes


class PDFPageTests(unittest.TestCase):
    def test_whole_page_jpeg_extraction_keeps_bytes_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scan.pdf'
            original_image = scan_pdf(path, double_matrix=True)
            original_pdf = path.read_bytes()
            outputs = {}
            result = convert_pdf_pages(path, lambda name, data: outputs.update({name: data}))
            self.assertEqual(result['pages'], 1)
            self.assertEqual(result['page_methods'][0]['method'], 'extracted_image')
            self.assertEqual(outputs['000001.jpg'], original_image)
            self.assertEqual(result['page_manifest'][0]['sha256'], hashlib.sha256(original_image).hexdigest())
            self.assertEqual(path.read_bytes(), original_pdf)

    def test_rotation_crop_overlay_and_colour_use_page_render(self):
        for options in ({'rotation': 90}, {'crop': True}, {'overlay': True}, {'colour': 'CMYK'}, {'default_colour': True}, {'interpolate': True}, {'optional_content': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'page.pdf'
                scan_pdf(path, **options)
                outputs = {}
                result = convert_pdf_pages(path, lambda name, data: outputs.update({name: data}))
                method = result['page_methods'][0]
                self.assertEqual(method['method'], 'rendered_page')
                self.assertEqual(method['dpi'], 200)
                with Image.open(io.BytesIO(outputs['000001.png'])) as image:
                    self.assertEqual(image.size, (method['width'], method['height']))
                    self.assertEqual(image.mode, 'RGB')
                    if options.get('rotation'):
                        self.assertGreater(image.width, image.height)

    def test_vector_text_transparency_and_annotation_render_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'pages.pdf'
            with pikepdf.new() as pdf:
                for index in range(2):
                    page = pdf.add_blank_page(page_size=(72, 108))
                    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
                        Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
                    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font),
                        ExtGState=pikepdf.Dictionary(GS1=pikepdf.Dictionary(ca=.5, CA=.5)))
                    page.Contents = pdf.make_stream(b'/GS1 gs 1 0 0 rg 10 10 40 50 re f BT /F1 12 Tf 5 80 Td (Text) Tj ET')
                    if index:
                        page.Rotate = 90
                pdf.save(path)
            outputs = {}
            result = convert_pdf_pages(path, lambda name, data: outputs.update({name: data}))
            self.assertEqual(list(outputs), ['000001.png', '000002.png'])
            self.assertTrue(all(m['method'] == 'rendered_page' for m in result['page_methods']))
            with Image.open(io.BytesIO(outputs['000001.png'])) as image:
                pixel = image.convert('RGB').getpixel((60, 200))
                self.assertGreater(pixel[0], pixel[1])
                self.assertGreater(pixel[1], 80)
            self.assertEqual(result['page_methods'][1]['rotation'], 90)

    def test_pdf_transform_readback_preserves_order_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'scan.pdf'
            scan_pdf(path)
            before = path.read_bytes()
            inspection = inspect_file(path, stable_seconds=.01)
            destination = root / 'output'; destination.mkdir()
            result = transform_archive(path, destination, b'<ComicInfo><Title>Fixture</Title></ComicInfo>', inspection['sha256'])
            self.assertEqual(result['page_methods'][0]['method'], 'extracted_image')
            with zipfile.ZipFile(destination / 'book.cbz') as archive:
                self.assertEqual(archive.namelist(), ['000001.jpg', 'ComicInfo.xml'])
            self.assertEqual(path.read_bytes(), before)

    def test_pixel_budget_blocks_render_before_tool_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'large.pdf'
            with pikepdf.new() as pdf:
                pdf.add_blank_page(page_size=(10000, 10000))
                pdf.save(path)
            with self.assertRaisesRegex(LibraryError, 'pixel'):
                convert_pdf_pages(path, lambda *_: None)
