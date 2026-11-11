# Third-party notices

The [MIT license](LICENSE) covers original Booksys code. It does not relicense
third-party code, data, container layers or downloaded works.

| Component | Scope and license | Source and retained notice |
| --- | --- | --- |
| cbzforge parser | Adapted parser and vendored reference; MIT, copyright 2026 Benoît Chevalier | [Pinned source and modifications](library/vendor/cbzforge/README.md), [license](library/vendor/cbzforge/LICENSE) |
| Byparr | Worker and adapter base; GNU GPL version 3 | [Exact upstream revision](https://github.com/ThePhaseless/Byparr/tree/b041dd2998110fc61789ae65e2506bd5b79edba5), [full GPL](third_party/Byparr-GPL-3.0.txt) |
| UnRAR | Library image, Debian `unrar` from `unrar-nonfree`; non-free redistribution license, Alexander Roshal | [Exact Debian copyright and component terms](third_party/unrar-copyright.txt), [source](https://sources.debian.org/src/unrar-nonfree/1%3A7.1.8-1/) |
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
