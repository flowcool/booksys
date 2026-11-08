"""Print a bounded runtime summary. Never print credentials or account logins."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml
from pipeline import Pyload

root = Path("/data/booksys")
credentials = json.loads((root / "secrets/pyload.json").read_text())
api = Pyload("http://127.0.0.1:8000", credentials["api_key"])
monitor = yaml.safe_load((root / "state/monitor.yaml").read_text())
queue = api.call("get_queue_data")
backfill_path = root / "state/backfill.yaml"
backfill = yaml.safe_load(backfill_path.read_text()) if backfill_path.exists() else None
backfill_summary = None if backfill is None else {
    "status": backfill["status"], "pages_requested": backfill["pages_requested"],
    "pages_captured": len(backfill["pages"]), "unique_items": len(backfill["items"]),
    "submitted": backfill["submitted"],
}
result = {
    "server": api.call("status_server"),
    "categories": monitor["categories"],
    "submitted_items": len(monitor["submissions"]),
    "backfill": backfill_summary,
    "fiches": None,
    "package_count": len(queue),
    "packages": [{"pid": p["pid"], "name": p["name"], "destination": p["dest"],
        "files": [{k: f[k] for k in ["fid", "name", "plugin", "status", "statusmsg", "error", "size"]}
                  for f in p["links"]]} for p in queue[-10:]],
    "downloaded_files": [{"name": str(p.relative_to(root / "downloads")), "bytes": p.stat().st_size}
                         for p in sorted((root / "downloads").rglob("*")) if p.is_file()][:50],
}
fiche_path = root / "state/fiches.yaml"
if fiche_path.exists():
    from collections import Counter
    progress = yaml.safe_load(fiche_path.read_text())
    result["fiches"] = {"tracked": len(progress["items"]),
                        "total": sum(e["status"] == "queued" for e in monitor["submissions"].values()),
                        "states": dict(Counter(e["status"] for e in progress["items"].values()))}
print(json.dumps(result, indent=2, ensure_ascii=False))
