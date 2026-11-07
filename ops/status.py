"""Print a bounded runtime summary. Never print credentials or account logins."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml
from pipeline import Pyload

root = Path("/data/booksys")
credentials = json.loads((root / "secrets/pyload.json").read_text())
api = Pyload("http://127.0.0.1:8001", credentials["api_key"])
monitor = yaml.safe_load((root / "state/monitor.yaml").read_text())
queue = api.call("get_queue_data")
result = {
    "server": api.call("status_server"),
    "categories": monitor["categories"],
    "submitted_items": len(monitor["submissions"]),
    "packages": [{"pid": p["pid"], "name": p["name"], "destination": p["dest"],
        "files": [{k: f[k] for k in ["fid", "name", "plugin", "status", "statusmsg", "error", "size"]}
                  for f in p["links"]]} for p in queue],
    "downloaded_files": [{"name": str(p.relative_to(root / "downloads")), "bytes": p.stat().st_size}
                         for p in sorted((root / "downloads").rglob("*")) if p.is_file()][:50],
}
print(json.dumps(result, indent=2, ensure_ascii=False))
