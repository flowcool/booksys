# PostgreSQL runtime

Booksys has one operational authority: PostgreSQL 18.6 or later.
Historical SQLite and YAML files remain migration inputs and retained proof.
Preserve original downloads and uncertain-effect journals.

| Image | Role |
| --- | --- |
| `booksys-worker` | Collector pipeline |
| `booksys-library` | Library CLI, HTTP controller and queue workers |
| `booksys-byparr` | Retained browser-session adapter |

`Dockerfile.stack` supplies the application image used by `compose.stack.yaml`.
The controller serves HTTP in its own process.
Collection, intake, metadata, copies and routing use work_items.
PostgreSQL and the single retained pyLoad remain separate dependencies.
`Dockerfile.pyload` adds the NAS guard to the downloader image.
It is not a fourth published Booksys package.

Compose requires explicit images, credentials, qualified NAS mounts and retained
dependency selections. Private DSN files must have mode 0600.
Inspect the selected configuration before any lifecycle action.
Never repeat an uncertain downloader or file write without reconciliation.
Keep recovery copies and proof until the outcome is known.

CI uses a disposable PostgreSQL service and synthetic temporary paths.
Several tests require `BOOKSYS_TEST_PG_DSN_FILE`; they do not all skip when it is absent.
Never point tests at an operator database.
Source publication and image release remain separate from runtime activation.
