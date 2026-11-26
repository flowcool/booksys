# Compose stack and UGreen storage

## Telegram inputs

Give the existing Booksys workers read-only access to the qualified UGreen
archive subtree and the local published receipt feed. Use explicit binds
with host-path creation disabled. The producer owns archive writes and private
downloader state. The worker uses one shared resolver for retained Telegram
sources during intake, metadata, copies and recovery.

Verify the selected mount and sample bytes through the worker paths. Keep the
archive bind while recorded origins reference it. See the
[rollback pair](../operations.md#pull-telegram-receipts).
