# Architecture

## Scope

The operator selected new entries in the Bookys **bandes dessinées / new** category.
Collect all download rows and all outgoing download anchors, including multiple links per row.
The automatic pipeline excludes entries at or below its saved starting ID.
This uses numeric IDs and descending catalogue order. It does not filter by a publication timestamp.
An older entry that is edited or promoted without a new ID is not a new entry for this pipeline.

Use local software for collection and downloading. Provider activation and credentials belong to each installation.
Byparr still visits the external site. Its upstream startup also retrieves egress and GeoIP information.
The initial comparison and the decision to keep pyLoad-ng are in [README](../README.md#downloader-decision).

## Components

| Component | Responsibility | Boundary |
| --- | --- | --- |
| `booksys.py` | Parse catalogues, visit item/intermediate pages, persist items and export URLs | Fetches Bookys pages through Byparr; never fetches hoster files |
| `pipeline.py` | Initialize and advance the category watermark; submit packages to pyLoad | Downloads begin only after URL readback and queue transfer |
| `Byparr` service | Run the browser and handle the site challenge | Pinned image in Compose |
| `pyload` service | Resolve hoster links, queue and download files | Separate config and downloads from Lunii |
| `worker` service | Run `pipeline.py` in a loop | Sleep 900 seconds after each cycle; this is not a cron schedule |
| `ops/` | Bootstrap, deploy and inspect the installation | Host commands target booksys-host |

`compose.yaml` provides a standalone browser for bounded manual collection.
`compose.runtime.yaml` owns the permanent pipeline and its data mounts.
The worker image reuses the pinned Byparr base. Only the `byparr` service starts its browser API.

## Collection and watermark

1. A new monitor reads the first catalogue page and records its maximum item ID.
   It queues nothing during initialization.
2. A later cycle checks disk reserve and pyLoad download availability.
3. Collection follows pagination until it encounters an ID at or below the watermark.
4. Each completed item is saved atomically. A later attempt skips completed items.
5. The pipeline submits all collected items above the old watermark.
6. It advances the watermark only after collection and submission finish.

An invalid page, changed ID ordering or missing pagination before the watermark stops the cycle.
The old watermark remains. Completed item records remain available for the next attempt.
Strict descending order is an assumption checked by the code, not a guarantee from Bookys.

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
| `/data/booksys/state/state.yaml` | Completed collection records, keyed by source URL |
| `/data/booksys/state/monitor.yaml` | Per-category watermark and submission journal |
| pyLoad config and database | Queue, accounts, plugin configuration and download progress |
| Host filesystem | Downloaded bytes and free space |
| Beads project `booksys` | Active work and delivery evidence |

`all-links.txt` and hoster text files are cumulative derived exports.
They are not submission journals. Reimporting them can create duplicates downstream.
File locks serialize each state directory. Atomic file replacement prevents a partial YAML write.
These mechanisms do not replace backups or coordinate manual edits with running services.

## Validation boundary

Offline tests use captured parser fixtures, a local HTTP server and an in-memory downloader.
An isolated pyLoad image test checked the real create/read/move API contract using a loopback target.
The initial production cycle had no new item. See the dated evidence in README and the owning Bead.
Live proof files are local and ignored by Git; losing this checkout loses those files.
Actual completion must be checked in pyLoad and on disk. A queued link or a partial file is insufficient.
