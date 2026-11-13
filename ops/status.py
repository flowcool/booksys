"""Print a bounded runtime summary. Never print credentials or account logins."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import Pyload

root = Path("/data/booksys")
credentials = json.loads((root / "secrets/pyload.json").read_text())
api = Pyload("http://127.0.0.1:8000", credentials["api_key"])
from library.operational_state import CollectorState
collector = CollectorState(root / "state")
monitor = collector.monitor()
from mirrors import logical_snapshot
queue = logical_snapshot(api, root / "state/mirror-routing.yaml")
result = {
    "server": api.call("status_server"),
    "categories": monitor["categories"],
    "submitted_items": len(monitor["submissions"]),
    "fiches": None,
    "package_count": len(queue),
    "packages": [{"pid": p["pid"], "name": p["name"], "destination": p["dest"],
        "files": [{k: f[k] for k in ["fid", "name", "plugin", "status", "statusmsg", "error", "size"]}
                  for f in p["links"]]} for p in queue[-10:]],
    "downloaded_files": [{"name": str(p.relative_to(root / "downloads")), "bytes": p.stat().st_size}
                         for p in sorted((root / "downloads").rglob("*")) if p.is_file()][:50],
}
progress = collector.fiches()
if progress["items"]:
    from collections import Counter
    result["fiches"] = {"tracked": len(progress["items"]),
                        "total": sum(e["status"] == "queued" for e in monitor["submissions"].values()),
                        "states": dict(Counter(e["status"] for e in progress["items"].values()))}
print(json.dumps(result, indent=2, ensure_ascii=False))
