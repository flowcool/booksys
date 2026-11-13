# Configurable resource limits

This public extract covers the two configurable deadlines.
These guards remain enabled.

| ID | Mechanism | Current limit | Reason |
| --- | --- | --- | --- |
| R27 Byparr request wall time | `booksys_core/browser.py`; `ops/byparr/session_api.py` | `BOOKSYS_BYPARR_BUDGET`, default 120 s; API accepts 1 to the configured maximum and uses that maximum when omitted | Include browser startup and navigation in one bounded solve. The client HTTP timeout adds 20 s for cleanup. |
| R37 Database total transaction wait | `library/postgres.py`, `Database.connection` | `BOOKSYS_DB_DEADLINE`, default 30 s | Connection, lock, query and commit share one remaining deadline. Explicit caller budgets and earlier enclosing deadlines take precedence. |

R27 comment: Include browser launch and navigation in the configured request
deadline, default 120 seconds.

Cookies stay in `Byparr.session` in memory. No cookie persistence is implemented.
See [operations](operations.md) for configuration and rollback.
