# Architecture

This tree records the simplified work_items lab runtime of 13 November 2026.
Use the [work identification scope](specs/0003-work-identification-and-metadata.md)
and the [glossary](../GLOSSARY.md) for the retained domain terms.

## Scope

The operator selected new entries in the Bookys **bandes dessinées / new** category.
Collect all download rows and all outgoing download anchors, including multiple links per row.
The automatic novelty pipeline excludes entries at or below its saved starting ID.
A separate, operator-authorized batch includes all items from the ten newest catalogue pages.
This uses numeric IDs and descending catalogue order. It does not filter by a publication timestamp.
An older entry that is edited or promoted without a new ID is not a new entry for this pipeline.

Use local software for collection and downloading. Keep provider credentials private.
Byparr still visits the external site. Its upstream startup also retrieves egress and GeoIP information.
The collector and downloader interfaces are summarized in [README](../README.md).

## Components

Local PostgreSQL is the only operational authority.
Compose defines the application, database, downloader and browser boundaries.

| Service | Responsibility | Boundary |
| --- | --- | --- |
| postgres | Operational state, policies and work_items | Local storage and private socket |
| controller | HTTP pages and catalogue observations | HTTP runs in the controller process |
| discovery | Discover obligations and refresh observations | Retained source policy and request budgets |
| collection, intake, metadata, copies, routing | Process work_items | Stage ownership and recoverable file operations |
| retirement | Reconcile completed packages | Retained terminal history before downloader removal |
| byparr | One browser session | Pinned session extension; no hoster downloads |
| pyload | Downloader queue and accounts | Sole writer of downloads |
| tools (opt-in) | Explicit operator CLI and recovery commands | Starts only on request |

Images contain application code and dependencies.
Historical SQLite/YAML files remain evidence. Runtime does not fall back to them.
External files retain media and immutable proof.
Reconcile uncertain operations before dependent effects.
Heavy media uses qualified NAS binds with exact identity and fresh health checks.
There is no local media fallback.

## Bookys browser session

`Dockerfile.byparr` adds `ops/byparr/session_api.py` to the pinned Byparr image.
The upstream revision checked on 8 November 2026 was
[`d28db93`](https://github.com/ThePhaseless/Byparr/tree/d28db932dd99bb0d56d1ddaff5ee0da1ac39ba14).
Its `/v1` handler still creates a new browser context for each request and has no
session parameter. Booksys keeps its browser factory and Cloudflare handler. A
small local entrypoint owns their lifetime. No second browser solver is added.

The runtime API retains one browser, context and page for the exact Bookys origin
`https://www7.bookys-ebooks.com`. Only `request.get` HTML requests are accepted.
Main-frame navigation to another origin is blocked. Covers keep their existing
same-site HTTP path. Cookies and browser storage stay in the transient browser
context. No persistent profile volume or exported cookie file is added. The response contract stays
compatible with `booksys.Byparr`, including the cookies needed for covers.

One caller can use the page at a time. Another caller receives HTTP 429 rather
than navigating the shared page. The collector retains its two-second pause.
Upstream media routes are replaced per request, so handlers cannot accumulate.
The browser is renewed after 256 successful requests, one hour of age, 30 minutes
without a request, or cgroup memory reaching 80% of its limit. These bounds are
local resource controls; they do not represent Cloudflare cookie expiry. A real
expired clearance can trigger the normal solver inside the retained context.

A timeout, solver failure or invalid HTML discards the context and fails the
request. There is no immediate retry or downloader resend. The next ordinary
collector cycle starts a fresh context and uses its existing journals. The total
request budget includes browser launch and is at most 60 seconds. Cleanup has a
five-second limit. Failed cleanup makes the service unavailable until restart.
`/health` reports local session-manager state without browsing an external site.
Logs contain generation, request count, duration and reset reasons, never cookie
values. Docker's 1536 MiB memory cap remains the hard limit.

The standalone manual `compose.yaml` keeps upstream Byparr. Only
`compose.stack.yaml` selects the Booksys session image. Tests for `/v1` run in
the pinned image without network or live mounts. The ordinary host suite skips
those image-specific tests. The bounded comparison script parses real catalogue,
item and link pages, but never follows an outgoing hoster URL or calls pyLoad.

## Collection and watermark

1. A new monitor reads the first catalogue page and records its maximum item ID.
   It queues nothing during initialization.
2. A later cycle checks pyLoad download availability.
3. Collection follows pagination until it encounters an ID at or below the watermark.
4. Each completed item is saved atomically. A later attempt skips completed items.
5. The pipeline submits all collected items above the old watermark.
6. It advances the watermark only after collection and submission finish.

An invalid page, changed ID ordering or missing pagination before the watermark stops the cycle.
The old watermark remains. Completed item records remain available for the next attempt.
Strict descending order is an assumption checked by the code, not a guarantee from Bookys.

## Ten-page batch

`backfill.yaml` stores the requested page count, saved catalogue pages, unique item URLs and progress.
The worker captures all ten pages before resolving items. It queues each resolved item immediately.
Completed collection records and submission confirmations are reused after a restart.
The batch shares the submission journal with the novelty monitor, so overlapping items are not resent.
The category watermark remains unchanged during the batch. Novelty monitoring resumes afterward.
A failure retains progress and retries the cycle after its normal pause.

## Submission and recovery

An item maps to one package. Its name starts with a deterministic hash of its source URL.
All unique URLs from every hoster are included. The collector does not select a preferred mirror.
pyLoad applies its own filename and duplicate handling. The pipeline does not verify content identity across mirrors.

The journal records these steps:

| Status | Meaning | Next normal action |
| --- | --- | --- |
| `creating` | Intent saved before package creation | Find the package by its exact name |
| `moving` | Package found and every URL verified | Move it to the download queue or confirm that it is there |
| `queued` | Queue destination confirmed | Skip further submission |

Creation uses pyLoad collector destination `0`. Transfer to queue destination `1` starts downloads.
GET API calls use query parameters. JSON content type is used only for writes.

After a lost response, the pipeline reads the collector and queue. It does not repeat an uncertain create.
A missing or ambiguous package requires investigation. A later cycle retries reconciliation reads.
This limits duplicate creation; it is not a distributed transaction or a guarantee of file completion.
Do not delete or rename packages involved in an unfinished submission.

## State ownership

| Source | Owns |
| --- | --- |
| Git / Compose / pinned images | Code and desired service configuration |
| Local PostgreSQL `booksys` | Collection records, watermarks, submission journals, backfill, fiche progress, catalogue, policies and work_items |
| Retained SQLite/YAML and immutable files | Historical proof and source comparison; never current operational authority |
| pyLoad config and database | Queue, accounts, plugin configuration and download progress |
| Host filesystem | Downloaded bytes and free space |

`all-links.txt` and hoster text files are cumulative derived exports.
They are not submission journals. Reimporting them can create duplicates downstream.
PostgreSQL transactions and ordered domain guards serialize operational changes.
Process locks retain exact worker ownership. Atomic file replacement protects
derived exports and immutable proof; it does not select business state.
These mechanisms do not replace backups or coordinate manual edits with running services.

## Offline reference

`booksys.extract_page` reads the source's `mainEntity`, `abstract`, detail rows
and `item-cover` selectors. It preserves supplied text and fields in each item's
`page` record. It does not infer authors or editions from prose.
`fiches.py` generates escaped French Markdown in a separate NAS sidecar directory.
Cover requests use Byparr's solved cookies and user agent, restricted to the
same Bookys host and `/storage/items/`. Redirects are validated before forwarding
cookies. Raster MIME/signature and an 8 MiB limit are checked. Covers need no
second browser or external solver. Cookies stay in memory.

Booksys mounts downloads read-only. Managed fiche/cover writes use the separate
NAS `work/collector-covers` bind at the collector's logical download path.
Unsafe package paths, symlinks and existing unmanaged fiche/cover files are
rejected. Random temporary files and atomic replacement protect writes.
Personal annotations belong outside these generated files.

Existing queued entries are enriched during the monitor's idle period, under
the pipeline lock. Page metadata is saved before cover transfer. Cover errors
retry after 15 minutes; browser-fetch failures stop browser work for that pause
and defer the affected entry for an hour. Other eligible entries can proceed
on later cycles. One in-flight item can extend the next monitor deadline.
No existing package is re-enqueued. Future packages receive text before queue
transfer. Completed documents refresh when pyLoad filenames change or a managed
file disappears. A manually removed pyLoad package requires intervention.

`catalogue.yaml` is a derived central export for LLMs. Source URLs are its keys.
It contains captured metadata, package/relative paths and a dated pyLoad file
snapshot. It is not a second authoritative queue or a live completion guarantee.
Source prose remains untrusted input to any consuming LLM.

## Validation boundary

Offline tests use captured parser fixtures, a local HTTP server and an in-memory downloader.
An isolated pyLoad image test checked the real create/read/move API contract using a loopback target.
Live proof files are local and ignored by Git; losing this checkout loses those files.
Actual completion must be checked in pyLoad and on disk. A queued link or a partial file is insufficient.
