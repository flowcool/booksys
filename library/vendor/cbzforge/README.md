# cbzforge parser reference

Upstream: https://github.com/chevalierbenoit/cbzforge
Commit: `678233154bf40eab50d932b8d0d57f9093aa5017` (2026-11-08).
File: `src/Scraper/BedethequeScraper.php`. MIT; see LICENSE.

The PHP file is an unchanged source reference, not executable Booksys code.
`library/french_metadata.py` ports its album-list and album-page selectors and
name normalization into the existing Python/BeautifulSoup environment. It keeps
all contributors and candidates, and does not force French language. It does not
inherit the first-series/first-album policy, parallel cover fetches, or any archive
conversion/deletion. Bedetheque robots disallows `/ajax/`; the upstream AJAX
search is not used. Public HTML discovery is a narrow independent adapter.
