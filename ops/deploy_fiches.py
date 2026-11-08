"""Deploy the authorized offline-fiche worker; preserve state and the old image."""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import Pyload


def run(*args: str) -> str:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True).stdout


os.umask(0o077)
assert run("docker", "info", "--format", "{{.Name}}").strip() == "booksys-host"
worker, browser, shared = json.loads(run("docker", "inspect", "booksys-worker-1", "booksys-byparr-1", "booksys-pyload"))
assert all(d["State"]["Running"] for d in [worker, browser, shared])
assert browser["State"]["Health"]["Status"] == "healthy"
mounts = {m["Destination"]: m["Source"] for m in shared["Mounts"]}
assert mounts["/downloads"] == "/data/booksys/downloads"
api = Pyload("http://127.0.0.1:8000", json.loads(Path("/data/booksys/secrets/pyload.json").read_text())["api_key"])
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
recovery = Path("/data/booksys/recovery") / ("fiches-" + stamp)
recovery.mkdir(mode=0o700)
tag = "booksys-worker:before-fiches-" + stamp.lower()
run("docker", "image", "tag", worker["Image"], tag)
(recovery / "previous-runtime.json").write_text(json.dumps({"worker_image": worker["Image"], "recovery_tag": tag,
                                                           "shared_container_id": shared["Id"]}, indent=2))
(recovery / "compose.runtime.yaml").write_text(run("git", "show", "prior-change:compose.runtime.yaml"))
print(f"Recovery: {recovery}", flush=True)
run("docker", "compose", "-f", "compose.runtime.yaml", "stop", "worker")
shutil.copytree("/data/booksys/state", recovery / "state")
before = {f["fid"] for p in api.call("get_queue_data") for f in p["links"]}
(recovery / "queue-file-ids.json").write_text(json.dumps(sorted(before)))
print("Worker stopped; state and original image preserved", flush=True)
print(run("docker", "compose", "-f", "compose.runtime.yaml", "build", "worker")[-1500:], flush=True)
run("docker", "compose", "-f", "compose.runtime.yaml", "up", "-d", "--no-deps", "worker")
after = {f["fid"] for p in api.call("get_queue_data") for f in p["links"]}
assert before <= after, "Existing queue files disappeared"
assert json.loads(run("docker", "inspect", "booksys-pyload"))[0]["Id"] == shared["Id"]
replacement = json.loads(run("docker", "inspect", "booksys-worker-1"))[0]
assert replacement["State"]["Running"]
assert {m["Destination"]: m["Source"] for m in replacement["Mounts"]}["/downloads"] == "/data/booksys/downloads"
proof = {"existing_queue_files_preserved": len(before), "shared_pyload_unchanged": True,
         "worker_running": True, "worker_image": replacement["Image"]}
(recovery / "proof.json").write_text(json.dumps(proof, indent=2))
print(json.dumps(proof), flush=True)
