# Booksys queues and activity

Status: accepted by the operator after seven design decisions and final confirmation
on 8 November 2026. This specification defines the outcome. It does not claim
that the dashboard is implemented or active. Beads owns delivery state.
Owner: `private-work-item`.

## Outcome

Add an **Activity** tab to the existing operator page. Help the operator answer:
what is working, what is waiting, and why? Link each stage to its affected items.
Keep this tab read-only. Existing identity-decision forms remain separate.

## Accepted decisions

1. Show current activity and queues in one place. Do not add controls that start,
   retry, pause, delete or prioritize work.
2. Use the existing operator page. Show a stage summary first and details on
   demand. Do not create another dashboard service or supervision stack.
3. Refresh the view every 30 seconds. Show when each source was observed. Mark
   stale, missing or unavailable observations. A refreshed page does not make an
   old source observation current.
4. For a waiting stage or item, show its reason, time waiting and next attempt
   when known. Make the affected items accessible. Use explicit unknown values
   when a source cannot provide these facts.
5. Keep significant events for the last 24 hours across restarts. Include work
   start/end, errors, recovery and blocking. Do not copy technical request logs.
   History starts at activation; do not invent events for earlier periods.
6. Show free space on `/data`, Byparr memory and the state of Booksys services in
   a compact resource strip. General machine monitoring stays in existing tools.
7. Show "No progress for X minutes" only when there is a known last progression.
   Do not infer a failure from elapsed time. Show a failure when explicit error
   evidence exists. If progression has never been observed, say so.

## Stage view

Cover these stages and their actual relationships:

| Stage | Required view |
| --- | --- |
| Bookys collection | New-entry or backfill scope, captured/transmitted/remaining items, active source, last progression |
| Byparr | Active request when known, Cloudflare solving, session renewal, last observed activity |
| pyLoad downloads | Active, waiting, finished and failed Booksys items, with supplied reasons |
| Local processing | Reception validation, extraction/conversion and OCR work or waiting inputs |
| Identification, classification and publication | Active phase, unresolved items, planned retries, operator decisions, resulting copies |
| Package retirement | Eligible, removed or retained acquisitions, with their reasons |

Each stage shows its state, active operation/item when known, meaningful last
progression and waiting count. Show the unit next to every count. Source entries,
packages, links, files and assets are different units. Do not add them into an
invented global total. Branching, duplicate assets and retired packages must not
appear as lost work because they no longer occupy the same queue.

Distinguish working, waiting, sleeping, stopped, blocked and unknown using source
evidence. Show the next scheduled attempt only when the producer or upstream
source supplies it. A busy lock is an observed wait, not a generic service error.
Do not promise remaining download time or retry deadlines absent from pyLoad.

Details identify the relevant items with readable titles, stable references,
reasons and observation times. Reuse existing fiches when available. Keep stale
values visible with their age; do not silently replace them with zero or empty.
The page stays responsive during a long cycle or source failure.

## Observation and history

Canonical collector journals, pyLoad, library state and retirement history keep
their existing ownership. Activity data is an observation, not another queue or
a replacement processing journal. Reuse canonical records for durable results.
Add the missing active phases, last meaningful progression, waits and lifecycle
events at their owning producers.

Store project-owned repeated observations and events in keyed YAML with stable
identifiers. Keep storage bounded by the rolling 24-hour history, record size and
significant-event policy. Reads and pruning must not take a business pipeline
lock or interfere with another producer. Preserve atomic writes and process-safe
ownership. Refreshing a projection or reporting a heartbeat is not progression.
After a restart, distinguish an old saved `running` value from observed service
activity. Keep last error, recovery and ordinary scheduled waits distinguishable.

Collect shared observations once, independently of the number of viewers. The
30-second browser refresh must not invoke pyLoad, scan all archives, perform OCR,
or run a library cycle. Reuse available snapshots and add only the bounded shared
upstream reads needed for missing activity. Respect the existing pyLoad API
budget and cadence. A failed observation must not stop collection or downloads.

Publish only an explicit list of allowed fields. Raw queue snapshots and routing
journals can contain passwords, private hoster URLs and credentials. Never expose
them through the page, its API, event history or logs. Treat source titles and
error text as untrusted data and render them safely. Do not add a public route,
new account or browser-accessible Docker socket.

## Existing sources and known gaps

The existing page already refreshes `/api/status` every 30 seconds. That endpoint
reads `evidence/dashboard.yaml` without calling pyLoad. The projection is saved
at cycle boundaries. `cycle.yaml` supplies cycle times and a coarse status, but
does not describe active substeps or provide a uniform heartbeat.

`backfill.yaml` and `monitor.yaml` supply captured items, submissions and last
success. `fiches.yaml`, the library catalogue, automation attempts, mirror-routing
and retirement journals supply outcomes and some retries. None is a complete
current-activity feed. Byparr `/health` supplies local availability, generation
and request count, but not a durable significant-event history.

The existing pyLoad snapshots do not provide every active transfer or retry
deadline. Service state, `/data` free space and Byparr memory need bounded local
observation. Verify the exact deployed interfaces before selecting these reads.
Do not call `ops/status.py` per page refresh; it reads the complete downloader
queue. The general 24-hour event history still needs implementation.

## Acceptance

- The existing operator page has an Activity tab with summary and item details.
  It shows all listed stages with actual units and distinct unknown values.
- A long-running cycle changes its activity view before cycle completion.
  Repeated page refreshes do not substitute for fresh source observations.
- The view refreshes every 30 seconds. Each source has its own observation time
  and a tested stale/unavailable state. Stopped services and restart leftovers
  cannot remain falsely current and running.
- Hoster delay, Cloudflare, occupied lock, active long cycle, manual decision,
  explicit error and no observed progression have representative cases. Waiting
  duration and next attempt are supplied only when supported.
- Progression timestamps advance on real stage work, not heartbeats or unchanged
  projections. A long wait alone never becomes an asserted failure.
- Significant events survive restart, retain their identity without duplicates,
  and expire after the rolling 24-hour window. No earlier history is fabricated.
- `/data` space, Byparr memory and observed service states are visible with units
  and freshness. Missing permissions or observations remain explicit.
- Multiple viewers do not multiply pyLoad polling or change its queue. HTTP
  refreshes acquire no processing lock and perform no business writes. Failures
  of observation do not stop processing.
- APIs, HTML and events omit private URL, password and credential fields. Titles
  and source error text cannot inject HTML or instructions.
- Existing processing, source files, submission reconciliation, retirement and
  operator decisions remain valid. Tests and integration use isolated state.
- Run relevant typechecks and agreed seam tests during implementation, one full
  final integrated suite and independent code review. Commit the owned scope.
  Record exact candidate, activation, live readback and recovery before lab use.
  Rollback retains current queues, journals, original files and event evidence.

## References

- [Architecture](../architecture.md)
- Operations
- Library operations
- Package retirement
- [Identification and metadata scope](0003-work-identification-and-metadata.md)
