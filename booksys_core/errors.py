"""Collection failures shared by adapters and policies."""

class CollectionError(Exception):
    """A fetch or document failed validation. No automatic retry."""

class BrowserError(CollectionError):
    """Stop browser work for this cycle after a solver or page-shape failure."""


class NativeRefusal(CollectionError):
    """The native API returned an explicit refusal rather than an uncertain response."""


class LibraryError(Exception):
    """A failed gate. Preserve evidence and stop dependent operations."""


class LibraryLockBusy(LibraryError):
    """A nonblocking lock was not acquired; the protected effect did not start."""


class ResultInputsChanged(LibraryError):
    """A prepared result no longer describes the current relevant inputs."""
