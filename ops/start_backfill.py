"""Record the operator-authorized ten newest catalogue pages as a durable worker job."""

import fcntl
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from booksys import DEFAULT_CATEGORY
from pipeline import save

directory = Path("/data/booksys/state")
with (directory / "pipeline.lock").open("w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    target = directory / "backfill.yaml"
    assert not target.exists(), "An existing backfill must be inspected, not overwritten"
    save(target, {"version": 1, "pages_requested": 10, "next_page": DEFAULT_CATEGORY,
                  "requested_at": datetime.now(timezone.utc).isoformat(), "status": "pending",
                  "pages": {}, "items": {}, "submitted": 0})
print("Recorded ten-page backfill; the worker will resolve and submit each item once")
