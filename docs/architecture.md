# Architecture

PostgreSQL owns operational state, catalogue, task policies, decisions and
Activity. Files retain media, immutable proof and derived exports. Historical
SQLite/YAML sources are migration inputs. Runtime never falls back to them.

The controller owns observations and confirmed decisions. Discovery finds
obligations. Seven independent queues own collection, intake, OCR, metadata,
copies, routing and retirement. OCR and metadata share one Supervisor container.
Each actor retains its own owner, heartbeat, durable grants and diagnostics.

The web uses read-only typed IPC. It has no catalogue credentials, media mounts
or Docker access. The restricted reader launcher alone receives Docker authority.
It verifies the selected image, exact input grants and stopped worker state.
Readers run without network and cannot write source downloads.

One Byparr session resolves Bookys pages. Requests stay sequential and obey
retained request budgets and remote deadlines. Failed browser cleanup requires
bounded session qualification. It cannot bypass an unrelated remote review block.

One existing pyLoad owns downloads and accounts. Submission intent precedes
creation. Lost responses trigger exact readback, never blind resend. Native
retirement records intent and reconciles receipts before later effects.
Terminal skipped, offline and failed mirrors do not count as pending transfers
and cannot admit unfinished files.

The catalogue uses work-level identification before edition resolution. Only
qualified local inputs enter admission. Published comic copies are CBZ, including
PDF sources. Source hints and provider candidates do not establish exact identity.
Existing accepted identification and metadata specifications define these rules.

See [runtime interfaces](runtime.md), [source identification](specs/0003-work-identification-and-metadata.md),
[image validation](image-validation.md) and [release recovery](releases.md).
Registry publication does not deploy this topology or prove runtime readiness.
