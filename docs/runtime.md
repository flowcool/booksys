# PostgreSQL runtime

Booksys has one operational authority: PostgreSQL 18.6 or later. Historical
SQLite and YAML files are migration inputs and retained proof. They are not a
runtime fallback. Preserve original downloads and uncertain-effect journals.

The three public images have these roles:

| Image | Roles |
| --- | --- |
| `booksys-worker` | Controller, discovery, seven queue actors, web, tools and restricted reader launcher |
| `booksys-library` | Networkless per-file archive, PDF and OCR readers |
| `booksys-byparr` | Retained browser-session adapter |

PostgreSQL and pyLoad are separate upstream dependencies. Keep one existing
downloader. The worker includes the Docker client for the restricted launcher.
Only that launcher receives a Docker socket. The other roles receive no Docker
authority. The web receives read-only typed IPC, not database credentials.

`compose.stack.yaml` is the complete topology. It requires explicit image,
credential, IPC, qualified NAS mount and retained dependency selections. It does
not create a ready installation from defaults. `ops/stack_config.py` generates
NAS contracts from resolved Compose and verified host mounts. Its files must
match the selected environment. Private DSN files must have mode 0600.

The application entrypoint is `/app/.venv/bin/python`. Compose selects modules
such as `library.stage_worker`, `ops.library_service`, `library.compose_web`
and `library.reader_service`. OCR and metadata share one processing container
under the fixed `ops/processing.conf` Supervisor configuration. A queue retains
its own owner, durable grants, heartbeat and diagnostics.

Use `ops/postgres_roles.py` after an explicit schema import to provision the
restricted application and launcher roles. `ops/postgresql_stack.py` manages
exact selected container identities and ordered lifecycle. Inspect its help and
the selected runtime record before any lifecycle action. Publication runs none
of these commands and does not deploy a stack.

The launcher validates exact inputs, worker image, native readiness and stopped
worker state before releasing grants. Store commit receipts resolve uncertain
SQL commits. Never repeat an uncertain native write. Keep recovery copies and
proof until the outcome is known. A responding web page alone does not prove
reader availability or processing progress.

CI uses a fresh PostgreSQL service, a synthetic catalogue and temporary paths.
Its database has no connection to an operator installation. Image probes run
without external network or real mounts. Source publication and image release
remain separate from runtime activation.
