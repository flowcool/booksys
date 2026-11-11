"""Supported writer startup: select authority, audit, then reconcile journals."""
from pathlib import Path
from library.catalog import Catalog


def verify_startup(directory: Path, *, audit: bool = True) -> None:
    catalog = Catalog(directory)
    backend = catalog.backend()
    if backend is not None and audit:
        from library.recovery import audit_ledger
        backend.audit()
        audit_ledger(backend, directory)
        catalog.project_pending()
