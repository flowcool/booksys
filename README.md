# Booksys

Local Bookys collector, pyLoad submission pipeline and portable comic-library tools. Source history records the development of the tool; it does not establish a running installation.

## Downloader support

Booksys submits collected link sets to pyLoad. The optional AllDebrid integration uses the provider's supported-hoster list; availability and quotas can change. Captcha support depends on pyLoad plugins and the operator's configuration. No provider account, activation, balance or successful challenge is implied.

## Collection and submission state

- `/data/booksys/state/state.yaml`: completed items, keyed by source URL.
- `/data/booksys/state/monitor.yaml`: category watermark and submission journal.
- `/data/booksys/state/backfill.yaml`: durable ten-page batch manifest and progress.
- `/data/booksys/state/all-links.txt`: every unique stored outgoing URL.
- `/data/booksys/state/<hoster>.txt`: separate exports by normalized hoster label.
- `/data/booksys/downloads/`: downloaded files, under one folder per package.

During the one-time batch, the worker first records exactly ten catalogue pages.
It then resolves and submits each item separately. A restart reuses saved pages and skips confirmed submissions.
The novelty monitor resumes after the batch completes.

The worker first creates a package in the pyLoad collector. It reads back every URL.
It then moves the package into the download queue and confirms the destination.
A deterministic package name lets it recover from a lost response without creating another package.
If the result cannot be reconciled, it stops the cycle. It does not automatically resend an uncertain create operation.
A later cycle retries reads. All completed item state survives interruption.
Stored links remain in state, including older links. Exports are cumulative.

## Standalone bounded collection

```sh
# Local browser for a manual bounded collection, without downloader submission.
docker compose up -d
uv sync --locked
uv run python booksys.py --pages 1 --max-items 2
```

This manual command includes existing entries. It does not use the new-entry watermark.
Default output directory: `data/`. Use `--category` repeatedly for several categories.
`--pages 0` follows all pages. `--max-items 0` has no item limit.
The default collects every hoster. `--hoster <label>` is an optional exact lowercase filter.
Requests are sequential, with a two second pause between them.
The collector extracts hoster URLs but never visits them itself.
The automatic downloader does visit and download those URLs.

