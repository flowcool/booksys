# Booksys

Local Bookys collector, pyLoad submission pipeline and portable comic-library tools.
This is a personal MIT project. Issues and contributions carry no support or
response-time commitment.

The current runtime uses PostgreSQL as its only operational authority. A
controller, independent queues, discovery, web and a restricted reader launcher
share the application image. Per-file archive, PDF and OCR readers run without
network. Byparr owns the retained browser session. The web reads typed IPC and
has no database credentials or Docker authority.

## Start here

- [Runtime roles and prerequisites](docs/runtime.md)
- [Public image interfaces](docs/public-images.md)
- [CI and contribution boundaries](docs/contribution-ci.md)
- [Release verification and recovery](docs/releases.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

The three intended amd64 packages are booksys-worker, booksys-library and
booksys-byparr under ghcr.io/flowcool. The first planned version is 0.1.0 from
the v0.1.0 prerelease, without latest. Verify actual published digests and
signatures before use. Source publication does not activate an installation.

compose.ghcr.example.yaml demonstrates CLI help. compose.stack.yaml records
the full topology with explicit images, credentials, qualified NAS storage,
IPC and retained dependency inputs. PostgreSQL and pyLoad are separate
dependencies. Preserve the existing downloader configuration and original files.

## Development

Install the locked development environment with uv sync --locked --extra dev.
Use uv run python booksys.py --help or uv run python -m library.cli --help.

The full isolated unittest suite requires PostgreSQL 18.6, native archive/PDF/OCR
tools and Node. Set BOOKSYS_TEST_PG_DSN_FILE to a mode-0600 file for a disposable
test database. Never select a shared or operator database. CI creates its own
temporary database and preserves synthetic evidence. Run the suite with
uv run python -m unittest discover -s tests -v. Type checks use
uv run mypy booksys.py pipeline.py fiches.py library booksys_core.

Keep credentials, actual source downloads and operational proof outside Git.
Never repeat an uncertain downloader or publication write. Retain its journals
and reconcile the exact current outcome first.
