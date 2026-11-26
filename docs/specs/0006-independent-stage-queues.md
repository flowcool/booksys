# Independent stage queues

## Telegram receipt discovery

The discovery role polls completed Telegram receipts at startup and every
30 seconds. Each turn checks one enabled scope and at most one page of
32 rows. Limit each row to 64 KiB. Telegram discovery runs independently of
pyLoad observations and the originals scan.

Registration commits the origin and initial intake work item together.
Intake validates the archived file. Normal business discovery then creates
metadata and copies obligations. Use the existing claims, events, retry
limits, and refusal paths. See [runtime operations](../operations.md#pull-telegram-receipts).
