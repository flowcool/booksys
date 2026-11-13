# Runtime configuration

Set these variables in the stack file under each service's `environment`.
Values must be positive integer seconds. Invalid values stop startup with an
error that names the variable. Each process reads them once at import time.
Recreate the affected services after a change. An image build is not required.

| Variable | Default | Effect | When to raise |
| --- | --- | --- | --- |
| `BOOKSYS_BYPARR_BUDGET` | 120 | Default client solve budget and API default/maximum. Includes browser startup and navigation. The client sends `maxTimeout` in milliseconds and allows another 20 seconds for HTTP cleanup. | A slow host cannot finish a valid solve within the current budget. Set the same value on the collection client and Byparr service. |
| `BOOKSYS_DB_DEADLINE` | 30 | Default total database transaction deadline. Connection, lock, query and commit share it. Explicit `seconds=` values and earlier enclosing deadlines still apply. | Valid transactions reach `storage_busy` on slow storage. Check contention before raising it. Set it on the application services that use PostgreSQL. |

The budget does not change the two-second Bookys pacing or browser cleanup limit.
Rollback: restore the previous values and recreate the affected services.

Solved cookies and the user agent remain in `Byparr.session` in process memory.
Direct HTML and cover requests reuse them. A process restart loses them.
The session API closes its browser after each solve. It does not persist cookies.

See the [limit audit](audit-limits.md) for the relevant resource guards.
