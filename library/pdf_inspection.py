"""PDF validation used by both the reader and conversion workers."""
from pathlib import Path
import re
from typing import Any
import pikepdf
from library.primitives import LibraryError
MAX_MEMBER = 64 * 1024 ** 2
MAX_EXPANDED = 2 * 1024 ** 3
MAX_PAGES = 2000

def inspect_pdf(path: Path) -> dict[str, Any]:
    with pikepdf.open(path, attempt_recovery=False) as pdf:
        if pdf.is_encrypted:
            raise LibraryError('Encrypted PDF')
        warnings_found = pdf.check_pdf_syntax()
        fatal_warnings = [warning for warning in warnings_found if not re.fullmatch(
            r'WARNING: [^\n]+: incorrect object count in outline hint table', warning)]
        if not 0 < len(pdf.pages) <= MAX_PAGES or fatal_warnings:
            raise LibraryError('Unreadable PDF or page limit')
        forbidden = {'/AA', '/JS', '/JavaScript', '/EmbeddedFiles',
                     '/EF', '/RichMedia', '/AcroForm', '/XFA', '/Launch', '/Collection'}
        for obj in pdf.objects:
            if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                if forbidden.intersection(str(key) for key in obj.keys()):
                    raise LibraryError('Active PDF content or attachment')
                for action_key in ('/A', '/OpenAction'):
                    if action_key not in obj:
                        continue
                    action = obj[action_key]
                    if (not isinstance(action, pikepdf.Dictionary) or str(action.get('/S', '')) != '/GoTo'
                            or set(map(str, action.keys())) - {'/S', '/D', '/Type'}):
                        raise LibraryError('Active or external PDF action')
                    destination = action.get('/D')
                    if (not isinstance(destination, pikepdf.Array) or len(destination) < 2
                            or destination[0].objgen not in {page.obj.objgen for page in pdf.pages}
                            or str(destination[1]) not in ('/Fit', '/FitB', '/XYZ', '/FitH', '/FitV', '/FitR', '/FitBH', '/FitBV')):
                        raise LibraryError('Unresolved or external PDF destination')
                if str(obj.get('/Type', '')) == '/Filespec':
                    raise LibraryError('PDF attachment')
        # Decode every stream. Syntax alone does not check compressed page bytes.
        expanded = 0
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    if str(obj.get('/Subtype', '')) == '/Image':
                        width, height = int(obj.get('/Width', 0)), int(obj.get('/Height', 0))
                        if width <= 0 or height <= 0 or width * height > 50_000_000:
                            raise LibraryError('PDF image pixel limit')
                        image = pikepdf.PdfImage(obj).as_pil_image()
                        if image.width * image.height > 50_000_000:
                            raise LibraryError('PDF image pixel limit')
                        image.load()
                        size = image.width * image.height * len(image.getbands())
                    else:
                        size = len(obj.read_bytes())
                    expanded += size
                    if size > MAX_MEMBER or expanded > MAX_EXPANDED:
                        raise LibraryError('PDF stream size limit')
                except pikepdf.PdfError as exc:
                    raise LibraryError('PDF stream cannot be validated') from exc
        for page in pdf.pages:
            list(pikepdf.parse_content_stream(page))
        embedded = {str(k).lstrip('/'): str(v) for k, v in pdf.docinfo.items()}
        return {'status': 'validated', 'format': 'pdf', 'pages': len(pdf.pages),
                'embedded': embedded, 'reader_warnings': warnings_found}
