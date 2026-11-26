# PostgreSQL operational state

## Telegram checkpoints

Use one `collection_checkpoints` key `telegram:<scope>` per enabled scope.
Store the stream ID and last completed page. Advance the page after every
row has a durable result or a verified prior result. Persist a block on a
damaged immutable page or identity conflict until the operator selects a
verified replacement stream.

Use the existing PostgreSQL origin and work item transaction. Reconcile an
uncertain commit by reading the exact origin and its intake work item.
No schema change is required. See [runtime operations](../operations.md#pull-telegram-receipts).
