"""One credential-free cover request with a parent-enforced absolute deadline."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from library.french_metadata import reference_url
from library.french_source import NoRedirect
from library.primitives import LibraryError

MAX_BYTES = 5 * 1024**2


def fetch_binary(url: str) -> tuple[int, bytes]:
    reference_url(url)
    try:
        child = subprocess.run([sys.executable, '-m', 'library.cover_transport', url],
            capture_output=True, timeout=20, check=True,
            env={'PATH': os.environ.get('PATH', os.defpath)})
        if len(child.stdout) > 8 * 1024**2:
            raise LibraryError('Cover transport output exceeds limit')
        result = json.loads(child.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        raise LibraryError('Cover transport unavailable or deadline exceeded') from exc
    from library.metadata import PROVIDER_BUDGET
    budget = PROVIDER_BUDGET.get()
    if budget is not None and (result['status'] == 429 or result.get('retry_after') is not None):
        budget.remote_deadline(result.get('retry_after'))
    if result.get('reason'):
        raise LibraryError(result['reason'])
    body = base64.b64decode(result.get('body', ''), validate=True)
    if len(body) > MAX_BYTES:
        raise LibraryError('Cover response exceeds limit')
    return int(result['status']), body


def main() -> None:
    result: dict[str, Any] = {'status': 0}
    try:
        url = reference_url(sys.argv[1])
        request = Request(url, headers={'User-Agent': 'Booksys/0.1 (personal metadata catalogue)',
                                        'Accept': 'image/jpeg,image/png,image/webp,image/gif'})
        with build_opener(ProxyHandler({}), NoRedirect).open(request, timeout=15) as response:
            if response.geturl() != url:
                raise LibraryError('Cover redirect rejected')
            if response.headers.get_content_type() not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
                raise LibraryError('Cover response is not a raster image')
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise LibraryError('Cover response exceeds limit')
            if not (body.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a'))
                    or body.startswith(b'RIFF') and body[8:12] == b'WEBP'):
                raise LibraryError('Cover signature is not a raster image')
            result.update(status=response.status, body=base64.b64encode(body).decode('ascii'))
    except HTTPError as exc:
        result.update(status=exc.code, retry_after=exc.headers.get('Retry-After'))
    except (OSError, ValueError, LibraryError) as exc:
        result['reason'] = str(exc)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
