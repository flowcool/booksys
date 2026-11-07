# Booksys

Local Bookys collector, pyLoad submission pipeline and portable comic-library tools. Source history records the development of the tool; it does not establish a running installation.

## Run

Use Python 3.11+ and Docker Compose on the same host. All solver requests stay on that host.
The browser visits Bookys. There is no remote solver or paid API.
The Compose image is pinned to the tested Byparr 3.1.1 digest.

```sh
docker compose up -d
uv sync --locked
uv run python booksys.py --pages 1 --max-items 2
```

Outputs:

- `data/state.yaml`: items keyed by source URL, titles, hoster metadata and resolved URLs.
- `data/1fichier.txt`: one unique 1fichier URL per line.
- `data/nitroflare.txt`: one unique Nitroflare URL per line.

Import **one** hoster file into pyLoad. Both files contain mirrors of the same items.
The collector never visits the hoster URLs. It does not check link availability.
No downloader credentials are needed. Downloader submission is not implemented.

## Categories and repeated runs

```sh
uv run python booksys.py \
  --category https://www7.bookys-ebooks.com/bandes-dessinees/new \
  --pages 2 --hoster 1fichier
```

Repeat `--category` to select several categories. The default is the URL above.
`--pages 0` follows all pages. `--max-items 0` has no item limit.
The default fetches one catalogue page and its items.
Each request runs sequentially, with a two second pause after the previous request.

Repeat the command from cron or a systemd timer to collect new entries.
Keep the same working directory, or set an absolute `--directory`.
Each run scans the selected catalogue pages. Completed items are skipped.
Exports contain all stored URLs for the selected hosters, not just the new URLs.
Do not repeatedly import the whole export unless the downloader deduplicates it.

An interrupted run retains completed items. The next run retries the unfinished item.
`--refresh` revisits completed items. Stored links remain in state, including older links.
Only one process can use a state directory at a time.

An unexpected catalogue, a remaining Cloudflare challenge, or a missing selected hoster stops the run.
The command exits with code 1 and keeps prior results. There is no automatic retry loop.
The collector requires at least one selected hoster on each item. It does not require every selected hoster to be present.

Cloudflare can change its challenge. The successful test does not guarantee future access.
A full archive scan was not tested. Start with a bounded scan.

