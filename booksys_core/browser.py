"""Local Byparr transport and trusted same-origin cover transport."""
from __future__ import annotations
from http.cookies import CookieError, SimpleCookie
import json
from pathlib import Path
import re
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from booksys_core.errors import BrowserError, CollectionError
from booksys_core.parsing import document, site_url
from booksys_core.browser_outcomes import BrowserOutcome, Outcome, retry_after
from booksys_core.diagnostics import REASONS

class Byparr:
    def __init__(self, endpoint: str, delay: float = 2, timeout: int = 60):
        address = urlsplit(endpoint)
        if (address.scheme != "http" or address.hostname not in {"localhost", "127.0.0.1", "::1", "byparr"}
                or address.username or address.password):
            raise CollectionError("Byparr must use a local HTTP endpoint")
        self.endpoint, self.delay, self.timeout = endpoint, delay, timeout
        self.last_request = 0.0
        self.session: dict[str, Any] = {}

    def __call__(self, url: str) -> str:
        return self.fetch_html(url, url, str(uuid.uuid4()))

    def check_session(self) -> str:
        """Qualify the retained service and one Bookys catalogue read, sequentially."""
        from booksys_core.parsing import DEFAULT_CATEGORY, catalogue
        from booksys_core.values import digest
        def health() -> dict[str, Any]:
            try:
                with urlopen(self.endpoint.rsplit('/', 1)[0]+'/health', timeout=5) as response:
                    value = json.loads(response.read(8193))
                if (value.get('status') != 'ok' or type(value.get('generation')) is not int
                        or type(value.get('requests')) is not int):
                    raise ValueError('Unqualified browser health')
                return {k: value[k] for k in ('status', 'generation', 'requests')}
            except (OSError, ValueError, TypeError, AttributeError):
                raise BrowserOutcome(Outcome('cleanup_failed', 'local')) from None
        before = health()
        html = self._fetch_byparr('cleanup-recovery', DEFAULT_CATEGORY, str(uuid.uuid4()))
        try:
            items, _ = catalogue(html, DEFAULT_CATEGORY)
            if not items:
                raise CollectionError('Empty recovery catalogue')
        except CollectionError:
            raise BrowserOutcome(Outcome('invalid_content', 'remote')) from None
        after = health()
        return 'byparr-session:' + digest({'before': before, 'after': after, 'items': len(items)})

    def fetch_html(self, source_ref: str, url: str, request_id: str) -> str:
        if self.session.get('cookies') and self.session.get('userAgent'):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            try:
                headers = {'User-Agent': self.session['userAgent'],
                           'Cookie': '; '.join(c['name'] + '=' + c['value'] for c in self.session['cookies'])}
                with urlopen(Request(url, headers=headers), timeout=30) as response:
                    raw = response.read(16 * 1024**2 + 1)
                    final_url = site_url(response.geturl(), url)
                    if response.status != 200 or len(raw) > 16 * 1024**2:
                        raise CollectionError('Invalid HTTP response')
                    html: str = raw.decode(response.headers.get_content_charset() or 'utf-8')
                    document(html)
                    if any(marker in html.lower() for marker in ('just a moment', 'cf-challenge', 'challenge-platform')):
                        raise CollectionError('Cloudflare challenge')
                    cookies = {c['name']: c for c in self.session['cookies']}
                    for header in response.headers.get_all('Set-Cookie', []):
                        parsed = SimpleCookie()
                        parsed.load(header)
                        for name, morsel in parsed.items():
                            cookies[name] = {**cookies.get(name, {'name': name,
                                             'domain': urlsplit(final_url).hostname, 'path': '/'}),
                                             'value': morsel.value}
                    self.session['cookies'] = list(cookies.values())
                return html
            except (OSError, ValueError, LookupError, CollectionError, CookieError) as exc:
                if isinstance(exc, HTTPError):
                    exc.close()
            finally:
                self.last_request = time.monotonic()
        return self._fetch_byparr(source_ref, url, request_id)

    def _fetch_byparr(self, source_ref: str, url: str, request_id: str) -> str:
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        payload = {"cmd": "request.get", "url": url,
                   "maxTimeout": self.timeout * 1000, "blockMedia": False}
        request = Request(self.endpoint, json.dumps(payload).encode(),
                          {"Content-Type": "application/json", 'X-Booksys-Request-ID': request_id})
        try:
            try:
                with urlopen(request, timeout=self.timeout + 20) as response:
                    raw = response.read(16 * 1024**2 + 1)
                    if len(raw) > 16 * 1024**2:
                        raise BrowserOutcome(Outcome('invalid_content', 'unknown'))
                    data = json.loads(raw)
            except HTTPError as exc:
                try:
                    raw = exc.read(8193)
                    detail = json.loads(raw).get('detail') if len(raw) <= 8192 else None
                    if (not isinstance(detail, dict) or detail.get('schema') != 1
                            or detail.get('request_id') != request_id
                            or detail.get('code') not in REASONS
                            or detail.get('origin') not in ('local', 'remote', 'unknown')
                            or detail.get('retry_after_state') not in ('absent', 'invalid', 'valid', 'unrepresentable')):
                        raise ValueError('Untrusted protocol failure')
                    code, origin = detail['code'], detail['origin']
                    if (code == 'browser_busy' and (origin != 'local' or exc.code != 429)
                            or code == 'remote_rate_limited' and origin != 'remote'):
                        raise ValueError('Inconsistent protocol origin')
                    delay = detail.get('retry_after_seconds')
                    if detail['retry_after_state'] == 'valid' and (type(delay) is not int or delay < 0):
                        raise ValueError('Invalid protocol retry delay')
                    outcome = Outcome(code, origin, detail['retry_after_state'], delay)
                except (ValueError, TypeError, AttributeError):
                    outcome = Outcome('unknown_browser_failure', 'unknown')
                finally:
                    exc.close()
                raise BrowserOutcome(outcome) from None
            solution = data.get("solution", {})
            if data.get("status") != "ok" or solution.get("status") != 200:
                raise BrowserOutcome(Outcome('unknown_browser_failure', 'unknown'))
            site_url(solution.get("url", ""), url)
            html = solution.get("response")
            if not isinstance(html, str):
                raise CollectionError("Byparr returned no HTML")
            try:
                document(html)
            except CollectionError:
                raise BrowserOutcome(Outcome('invalid_content', 'remote')) from None
            self.session = {k: solution.get(k) for k in ["url", "cookies", "userAgent"]}
            return html
        except BrowserOutcome:
            raise
        except (OSError, ValueError, CollectionError) as exc:
            raise BrowserOutcome(Outcome('browser_timeout' if isinstance(exc, TimeoutError)
                                         else 'unknown_browser_failure', 'unknown')) from None
        finally:
            self.last_request = time.monotonic()

    def cover_needs_session(self, source: str) -> bool:
        return (urlsplit(str(self.session.get('url', ''))).netloc != urlsplit(source).netloc
                or not self.session.get('userAgent'))

    def cover(self, url: str, source: str) -> tuple[bytes, str]:
        """Fetch only same-site Bookys covers using the solved browser identity."""
        def validate(target: str) -> None:
            site_url(target, source)
            if (not re.fullmatch(r"www\d*\.bookys-ebooks\.com", urlsplit(target).hostname or "")
                    or not urlsplit(target).path.startswith("/storage/items/")):
                raise CollectionError("Cover must use Bookys item storage")
        validate(url)
        if urlsplit(str(self.session.get("url", ""))).netloc != urlsplit(source).netloc:
            self(source)

        class Redirect(HTTPRedirectHandler):
            def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: str) -> Any:
                validate(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        host = urlsplit(source).hostname or ""
        cookies = [c for c in (self.session.get("cookies") or [])
                   if host == c["domain"].lstrip(".") or host.endswith("." + c["domain"].lstrip("."))]
        if not self.session.get("userAgent"):
            raise CollectionError("Browser returned no cover identity")
        headers = {"User-Agent": self.session["userAgent"], "Referer": source,
                   "Cookie": "; ".join(c["name"] + "=" + c["value"] for c in cookies)}
        try:
            with build_opener(Redirect()).open(Request(url, headers=headers), timeout=30) as response:
                data = response.read(8 * 1024**2 + 1)
                mime = response.headers.get_content_type()
        except HTTPError as exc:
            if exc.code == 429:
                state, delay = retry_after(exc.headers.get('Retry-After'), received_at=time.time())
                exc.close()
                raise BrowserOutcome(Outcome('remote_rate_limited', 'remote', state, delay)) from None
            if exc.code == 403:
                self.session = {}  # Solve again on a later retry, not an endless stale-cookie loop.
            raise
        signatures = {"image/jpeg": (".jpg", data.startswith(b"\xff\xd8\xff")),
                      "image/png": (".png", data.startswith(b"\x89PNG\r\n\x1a\n")),
                      "image/gif": (".gif", data[:6] in {b"GIF87a", b"GIF89a"}),
                      "image/webp": (".webp", data[:4] == b"RIFF" and data[8:12] == b"WEBP")}
        extension, valid = signatures.get(mime, ("", False))
        if not valid or len(data) > 8 * 1024**2:
            raise CollectionError("Invalid or oversized cover image")
        return data, extension
