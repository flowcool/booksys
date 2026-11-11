# Contributing

Booksys is a personal beta project. Issues and PRs are welcome. The maintainer
makes no support, review-time or compatibility commitment. See the [MIT license](LICENSE)
and [third-party notices](THIRD_PARTY_NOTICES.md). Submit only content you may share.

Use synthetic examples. Do not upload comics, captured private pages, credentials,
real hoster targets, local paths or browser sessions. Read [the security policy](SECURITY.md)
for private reports. Public metadata is not automatically licensed for redistribution.

Describe the problem and expected behavior before changing functional rules.
Keep patches small. Preserve current Python >=3.11 compatibility and runtime design.
Byparr owns browser automation. Tests must not visit hosters, submit downloads,
access the live lab or write to shared tracking state.

Install the locked development environment with `uv sync --locked --extra dev`.
Run the affected unittest files and `uv run mypy booksys.py pipeline.py fiches.py`.
The full offline suite uses `uv run python -m unittest discover -s tests -v`.
Native library tests need Node, bubblewrap, UnRAR, Tesseract (English and French),
Poppler and fonts. See [image validation](docs/image-validation.md) for exact checks.
Missing critical tools must fail acceptance. State which checks you ran.

Fork PRs use read-only tokens without provider secrets. The [CI policy](docs/contribution-ci.md)
explains required checks and image scope. This publication scope uses no external
automated reviewer. The maintainer reviews the patch and validation proof. PRs
require current green checks, an up-to-date base and resolved review threads.

Merges are manual. Dependency PRs follow the same checks. A release publishes images;
it does not deploy an installation. See [public image examples](docs/public-images.md).

Documentation-only pull requests run the required repository checks.
