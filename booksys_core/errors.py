"""Collection failures shared by adapters and policies."""

class CollectionError(Exception):
    """A fetch or document failed validation. No automatic retry."""

class BrowserError(CollectionError):
    """Stop browser work for this cycle after a solver or page-shape failure."""


class LibraryError(Exception):
    """A failed gate. Preserve evidence and stop dependent operations."""


class LibraryLockBusy(LibraryError):
    """A nonblocking lock was not acquired; the protected effect did not start."""
