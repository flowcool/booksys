# Third-party notices

The [MIT license](LICENSE) covers original Booksys code. It does not relicense
third-party code, data, container layers or downloaded works.

| Component | Scope and license | Source and retained notice |
| --- | --- | --- |
| cbzforge parser | Adapted parser and vendored reference; MIT, copyright 2026 Benoît Chevalier | [Pinned source and modifications](library/vendor/cbzforge/README.md), [license](library/vendor/cbzforge/LICENSE) |
| Byparr | Worker and adapter base; GNU GPL version 3 | [Exact upstream revision](https://github.com/ThePhaseless/Byparr/tree/b041dd2998110fc61789ae65e2506bd5b79edba5), [full GPL](third_party/Byparr-GPL-3.0.txt) |
| UnRAR | Worker and library images, Debian `unrar` from `unrar-nonfree`; non-free redistribution license, Alexander Roshal | [Exact Debian copyright and component terms](third_party/unrar-copyright.txt), [source](https://sources.debian.org/src/unrar-nonfree/1%3A7.1.8-1/) |
| Python packages | Installed from the pinned `uv.lock`; their own licenses apply | [Inventory](docs/license-inventory.yaml); package metadata and bundled license files remain in the images |
| Native tools and base layers | Debian/Ubuntu packages, browser, Python interpreter, fonts and libraries; multiple licenses | Image SBOM and `/usr/share/doc/*/copyright`; [distribution source requirements](docs/redistribution.md) |
| BnF fixtures | Bibliographic metadata, Bibliothèque nationale de France; Licence Ouverte | Source ARKs remain in each `tests/fixtures/bnf-*.xml`; [BnF terms](https://www.bnf.fr/fr/conditions-de-reutilisations-des-donnees-de-la-bnf). Retrieval: November 2026. No digitized work is included. |
| Parser fixtures | Handwritten minimal protocol markup and factual reference values | [Fixture provenance](tests/fixtures/provenance.json), [bibliographic provenance](tests/fixtures/french-metadata/provenance.json). No cover, review, comic page or descriptive prose is included. Source hashes record origin, not a license grant. |

UnRAR can be distributed inside another package under its own terms. It is not
MIT or open source. Its source must not be used to recreate the proprietary RAR
compression algorithm. Keep its full notice and the embedded component notices.
Booksys only calls the separate UnRAR executable to read archives.

Byparr is distributed as GPL-covered code. Keep its license and make the exact
Corresponding Source available alongside image downloads, with build scripts and
changes. An OCI source label or a link to a moving branch does not satisfy this
release requirement. See [the release-source checklist](docs/redistribution.md).
Other installed packages retain their individual terms. An SBOM or a vulnerability
scan is an inventory aid; neither proves license permission.

Do not infer rights to comics, covers, source-site content or user downloads from
this repository's MIT license. The operator must have the necessary rights.

## Native runtime notices

The exact standalone Python build and uv license texts are retained under
[third_party/native](third_party/native/manifest.json). The manifest records each
upstream revision URL and SHA256. The qpdf notice matches version12.4.2.
Distribution and wheel notices remain in each image. The reviewed source ledger
is [release/sources.lock.json](release/sources.lock.json); release qualification
compares it with the exact built inventory and retains full corresponding source.

The source-built psycopg3.3.8 and psycopg-c3.3.8 licenses, Supervisor4.3.0
terms and distribution libpq5 notice are retained in the
[library C-backend notice manifest](third_party/native/library-c-backend-manifest.json).
The C backend uses the distribution PostgreSQL client library. It does not ship
the additional native-library bundle from the psycopg-binary wheel.

The worker UnRAR and Poppler runtime dependency notices are retained in the
[worker decode notice manifest](third_party/native/worker-decode-manifest.json).
The manifest covers the exact added packages and their distribution sources.

## Cover confirmation

The SIFT matching function in `library/cover_sift.py` is adapted from BDneX
0.10.0, commit `9de29a73859a527a0c8ab4b0173d2f1e0e1e6f33`,
`bdnex/importer/covers.py`, by Laurent Besnard. The upstream distributed
[LICENSE](third_party/bdnex-cover-LICENSE.txt) is CC0-1.0. Its package metadata
also declares MIT. Preserve this provenance distinction. Booksys retains the
600-pixel height, grayscale, Lowe ratio 0.6, FLANN and scoring formula. It adds
input/resource bounds, deterministic RNG and receipts. BDneX is not installed
in the Python 3.14 worker.

OpenCV Python headless 4.13.0.92 includes MIT packaging terms, Apache-2.0
OpenCV terms and bundled native component terms. NumPy 2.4.6 includes BSD-3-Clause
and bundled native component terms, including OpenBLAS and runtime libraries.
The exact wheel notices are retained under [cover notices](third_party/native/cover/manifest.json).
They remain installed in the worker and library images. The locked amd64
wheels are `cp37-abi3-manylinux_2_28_x86_64` (OpenCV, compatible with CPython 3.14)
and `cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64` (NumPy).
`uv.lock` records their hashes. Image builds reject source builds for both.
Release qualification still requires the exact built inventory, bundled-native
source review, SBOM and vulnerability gates. This local change is not release proof.
