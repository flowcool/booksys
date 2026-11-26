# Runtime configuration

Set these variables in the stack file under each service's `environment`.
Values must be positive integer seconds. Invalid values stop startup with an
error that names the variable. Each process reads them once at import time.
Recreate the affected services after a change. An image build is not required.

| Variable | Default | Effect | When to raise |
| --- | --- | --- | --- |
| `BOOKSYS_BYPARR_BUDGET` | 120 | Default client solve budget and API default/maximum. Includes browser startup and navigation. The client sends `maxTimeout` in milliseconds and allows another 20 seconds for HTTP cleanup. | A slow host cannot finish a valid solve within the current budget. Set the same value on the collection client and Byparr service. |
| `BOOKSYS_BYPARR_HARD_DEADLINE` | budget + 25 (145) | Absolute limit for one Byparr request, cleanup included. Must be at least budget + 20. A watchdog thread exits the process with status 1 when it expires, so the restart policy brings a clean browser. | Keep it above the longest valid solve. Set it on the Byparr service only. |
| `BOOKSYS_BYPARR_LOOP_SILENCE` | hard deadline (145) | Longest allowed gap between event-loop heartbeats, idle time included. A stalled loop makes the watchdog exit the process with status 1. | Only if a valid blocking step exceeds it. Set it on the Byparr service only. |
| `BOOKSYS_DB_DEADLINE` | 30 | Default total database transaction deadline. Connection, lock, query and commit share it. Explicit `seconds=` values and earlier enclosing deadlines still apply. | Valid transactions reach `storage_busy` on slow storage. Check contention before raising it. Set it on the application services that use PostgreSQL. |

The budget does not change the two-second Bookys pacing or browser cleanup limit.
Rollback: restore the previous values and recreate the affected services.

The Byparr health check is observational: Docker does not restart a container that is only
`unhealthy`. The watchdog is the restart trigger. A native call that holds the Python GIL
can still stop the watchdog thread; an external guard would be needed for that case.
Use `curl --connect-timeout 2 --max-time 5` in the health check, inside Docker's 10 s timeout.
A failed cleanup recovery probe emits one `collector` / `byparr` / `degraded` event with the next attempt time.

Solved cookies and the user agent remain in `Byparr.session` in process memory.
Direct HTML and cover requests reuse them. A process restart loses them.
The session API closes its browser after each solve. It does not persist cookies.

See the [limit audit](audit-limits.md) for the relevant resource guards.

## Pull Telegram receipts

Deploy a compatible worker with pull disabled first. Mount the qualified
Telegram archive at `/data/booksys/telegram-archive` on discovery, intake,
metadata and copies. Mount the published feed at
`/data/booksys/telegram-receipts`. Use read-only binds with
`create_host_path: false`. Keep the producer's private state outside Booksys.
Verify the export, worker UID/GID and sample hashes through the worker paths.
Record the worker tag and the disabled stack commit as the rollback pair.

Freeze the prior managed origin and intake IDs at cutover. Include both
historical trials and all completed manual lots. Keep the lot evidence.
Enable only on discovery with `BOOKSYS_TELEGRAM_PULL=1` and
`BOOKSYS_TELEGRAM_SCOPES=BD_X18,bd_fr`. Pull starts disabled by default.
Add `webtoons-vf` after acceptance of the manual 50-chapter pilot and
confirmation that the producer limits its feed to the confirmed manifest.

Discovery polls at startup and every 30 seconds. Each poll selects one scope
in turn and reads its `head.json` and exact next page. A page has at most
32 rows and each UTF-8 JSON Lines row is at most 64 KiB including the newline.
The producer uses schema 1. Each scope directory contains a head with
`schema`, `stream_id` and `ready_page`, plus numbered pages such as
`00000001.jsonl`. Each row provides `schema`, `peer`, `topic`, `message`,
`path`, `original_name`, `size`, `sha256`, `completion_proof` and
`completion_ref`. Webtoon rows can also provide `origins`, `reposts`,
`index_title` or `index_review`. The stored path adds the
`telegram-archive/` prefix to the feed's archive-relative path.

Use Activity diagnostics for discovery. The event detail gives the scope and
page. A current head is idle. A missing head or committed page retries on its
next turn. A damaged page or conflicting receipt blocks that scope. Ask the
producer to verify a new stream. Disable pull during the stream change. Select
the new stream with `BOOKSYS_TELEGRAM_STREAMS=BD_X18=<stream-id>` on discovery
and enable pull. Use comma-separated `scope=stream-id` entries for several
scopes. The selected stream starts at page 1 and reconciles previous origins
by message identity. Keep the failed stream and its proofs.

A missing or unreadable archive file waits in intake. A size or SHA-256
mismatch uses the bounded intake retry path. Keep the receipt, original and
prior result for investigation. Both managed and archive sources use the
normal metadata and copies queues after intake. The 1 GiB per-comic limit,
source retention and CBZ publication rules apply.

To disable pull after an archive origin exists, restore the disabled stack
commit and its compatible worker tag. Keep the archive bind, PostgreSQL
state, originals, copies and receipts. Intake and downstream readers still
use recorded archive paths. Re-enable with the same stream to resume at the
last committed page. Before any archive origin exists, the pre-change worker
tag and stack commit are also a valid rollback pair.

## Submit completed Telegram documents

Prepare a private JSON Lines file with one line per completed document.
Use an approved scope. Start with an explicit list of 50 files. Review intake
errors and copy outcomes before submitting the rest. Check identification yield
for BD_X18 and bd_fr. Check chapter grouping separately for WEBTOONSVF.

Stage each completed file under
`$BOOKSYS_ORIGINALS_ROOT/booksys/telegram/<channel>_<message>/`.
Keep the path stable after submission. Booksys reads that exact file through
the originals mount. Keep the archive and the retained trial files in place.
Use the size and SHA-256 from the producer's completed-file proof. A tdl
export alone only establishes the message and original filename.

Each manifest line contains:

- `receipt` with the numeric channel `id` and a one-element `messages` array.
  The message has its numeric `id`, original filename in `file`, optional
  `type: "message"`, and optional caption in `text`. Use the same shape to
  select one message from a batch export.
- `topic` with the numeric topic ID, or `null` for the channel feed.
- `path` relative to `BOOKSYS_ORIGINALS_ROOT`. The staged basename may use
  tdl's sanitized form. Keep the same CBR, CBZ or PDF extension.
- `completed: true`, `size` in bytes, and the lowercase hex `sha256` of
  the completed file.
- For WEBTOONSVF, copy `index_title` from the matching message's
  `WEBTOONS_LISTS` entry in the retained tdl index. Use the index title when
  the entries agree. Leave it absent when the index does not establish one
  title. Booksys keeps this field as untrusted provenance. It sends missing
  titles, missing chapter numbers and conflicts to review.

Use the intake worker container for the selected Booksys instance. Set
`BD_X18_CHANNEL`, `BD_FR_CHANNEL` and `BD_FR_TOPIC` from the private receipt
inventory. The dry run checks the full list and prints the exact entries it
would submit. It opens no database connection.

```sh
docker exec -i "$BOOKSYS_INTAKE_CONTAINER" /app/.venv/bin/python -m library.telegram_intake \
  /dev/stdin --scope "$BD_X18_CHANNEL" --scope "$BD_FR_CHANNEL:$BD_FR_TOPIC" \
  --dry-run < first-50.jsonl
```

For the WEBTOONSVF channel feed, set `topic` to `null` and use this command.

```sh
docker exec -i "$BOOKSYS_INTAKE_CONTAINER" /app/.venv/bin/python -m library.telegram_intake \
  /dev/stdin --scope 1288796629 --dry-run < webtoon-first-50.jsonl
```

For the authorized submission, replace `--dry-run` with
`--state-dir /data/booksys/library-lab/state`, or use the selected worker's
`<root>/state` path. Each result reports `submitted` or `already-submitted`.
Keep the receipt and paths identical when retrying a batch. The command
validates the list before it writes. Each accepted line commits separately,
so a later database failure can leave earlier lines queued. A changed receipt
for an existing channel and message stops the batch with an error.

The manifest limit is 4096 lines. The single-comic reader applies its
existing 1 GiB validation ceiling during intake. A queued file can still fail
technical validation. To return to archive-only operation, stop submitting
lists and retain the files, receipts and existing tasks.

## Look up and review webtoon series

Use the state directory shared by the selected instance's workers. Pass each
confirmed index title as one argument. The lookup reuses each stored decision.
It sends the title to MangaUpdates and reads the accepted series record for
credits. Requests run in sequence with at least one second between starts and
up to three attempts per request. On provider failure, rerun the command later
to continue after the titles already saved.

```sh
python -m library.mangaupdates --state-dir "$BOOKSYS_STATE_DIR" lookup 'Index title' 'Another title'
python -m library.mangaupdates --state-dir "$BOOKSYS_STATE_DIR" review
python -m library.mangaupdates --state-dir "$BOOKSYS_STATE_DIR" review 'Index title'
python -m library.mangaupdates --state-dir "$BOOKSYS_STATE_DIR" accept 'Index title' 123 --by "$USER"
python -m library.mangaupdates --state-dir "$BOOKSYS_STATE_DIR" reject 'Another title' 456 --by "$USER"
```

Use a candidate's `mu_id` from the review output. Acceptance can read the
candidate's series record for credits. Rejection leaves the candidates available
for later review. Keep `webtoon-series.yaml` with the Booksys state backups.

The copy worker applies accepted references to ComicInfo on its next discovery
pass. It uses the same journal and recovery procedure as other managed copies.
For the identification yield, divide `identified_assets` by
`identification_attempted_assets` in `/api/status` when the denominator is
positive. Read `webtoon_assets`, `webtoon_review_assets` and `webtoon_copies`
separately.

## Copy-journal lookup index

Before activating the indexed copy-journal reader on an existing authority,
stop the application roles and run from the candidate image with the migration
credential: `python -m library.copy_operation_index --dsn-file <private-migration-dsn> --schema booksys`.
This explicit, repeatable operation creates `copy_operation_asset_v1(text)` and
`copy_operations_asset_v1`. It reads existing journals to build the index. It
does not rewrite journal payloads or change task outcomes. Fresh isolated stores
install the same index at creation. The function body is versioned and must not
be replaced under an existing index.

The reader selects only the requested asset and malformed ownership scopes.
Malformed unrelated journals still stop recovery through the existing checks.
If the install fails, its transaction rolls back; do not activate the new reader.
For application rollback, restore the preceding image through normal activation
and retain this additive function/index. No historical conversion is required.


## Operational schema 2 conversion

Schema 2 stores operational scalars and queried collections in typed columns
and keyed relations. Existing raw payloads, journals, source evidence, hashes
and file references remain intact. Exact raw values still fence completions.
Workers require schema 2. They never convert schema 1 at startup. Old workers
reject schema 2. Do not mix writer versions.

Run the explicit conversion from the exact candidate with all application
writers stopped. Use a consistent isolated database copy first. Retain the
source dump and technical recovery set. The credential must belong to a
migration role and use an owned private file. Use a new report path in an
existing writable directory. The qualified conversion budget is 1800 seconds;
the CLI default is 600 seconds.

```sh
python -m library.operational_conversion \
  --dsn-file /private/migration.dsn \
  --schema booksys \
  --authority YOUR_AUTHORITY_ID \
  --writers-stopped \
  --seconds 1800 \
  --report /private/proof/operational-schema-2.json
```

The command locks the existing tables. It converts in one transaction. It
validates promoted values, ordered relations and current admission manifests.
It compares every original column value and raw payload hash before and after
conversion. It installs change-token triggers and selects schema 2 only after
these checks. It does not call providers, native download services or media
readers. It does not reset retries, deadlines or uncertain operations.

An incompatible value stops conversion with its table and row key. Keep the
diagnostic and resolve the value's accepted semantics before retrying. A failed
transaction leaves schema 1 and its rows unchanged. A report that only says
`conversion_started` is not success. If commit acknowledgement is lost, read
back the schema marker and retain the report before any dependent action.

After successful conversion, bind the exact candidate with the existing
activation command while roles remain stopped. Recreate the selected roles
through the deployment runbook. Verify the delivered version and business
progress. This conversion command does not authorize a live migration or start
workers. After schema selection or new writes, retain later data and effects
and repair forward. Do not restore the old dump over later business writes or
restart an old image against schema 2.

The focused conversion test is `tests.test_operational_conversion`. Set
`BOOKSYS_TEST_PG_DSN_FILE` to a private credential for a throwaway PostgreSQL
18.6 instance. The tests create separate random schemas. Never point this
variable at shared mutable state.
