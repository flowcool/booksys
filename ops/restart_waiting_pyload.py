"""Release one authorized, zero-byte Booksys wait; retain all queue IDs and files."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import Pyload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file_id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    target = subprocess.check_output(
        ["docker", "info", "--format", "{{.Name}}"], text=True
    ).strip()
    if target != "booksys-host":
        raise RuntimeError("Unexpected Docker target")
    credentials = json.loads(Path("/data/booksys/secrets/pyload.json").read_text())
    api = Pyload("http://127.0.0.1:8000", credentials["api_key"])
    queue = api.call("get_queue_data")
    package = next(p for p in queue if any(f["fid"] == args.file_id for f in p["links"]))
    active = next(f for f in api.call("status_downloads") if f["fid"] == args.file_id)
    if not package["name"].startswith("Booksys-") or package["dest"] != 1:
        raise RuntimeError("Target is not a queued Booksys package")
    monitor = yaml.safe_load(Path("/data/booksys/state/monitor.yaml").read_text())
    if not any(entry.get("status") == "queued" and entry.get("package_id") == package["pid"]
               and entry.get("name") == package["name"] for entry in monitor["submissions"].values()):
        raise RuntimeError("Package is not a confirmed collector submission")
    if active["statusmsg"] != "waiting" or any(active[k] != 0 for k in ("size", "bleft", "speed")):
        raise RuntimeError("Target is not an empty waiting transfer")
    downloads = Path("/data/booksys/downloads").resolve()
    folder = (downloads / package["folder"]).resolve()
    if not folder.is_relative_to(downloads):
        raise RuntimeError("Package folder is outside downloads")
    if folder.exists() and any(p.is_file() and ".chunk" in p.name for p in folder.rglob("*")):
        raise RuntimeError("Package contains a partial download")
    ids = sorted(f["fid"] for p in queue for f in p["links"])
    files_before = {str(p.relative_to(folder)): p.stat().st_size for p in folder.rglob("*") if p.is_file()}
    proof = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "file_id": args.file_id,
        "package_id": package["pid"],
        "package_has_finished_mirror": any(f["statusmsg"] == "finished" for f in package["links"]),
        "preconditions_verified": True,
        "applied": False,
    }
    if args.apply:
        recovery = Path("/data/booksys/recovery") / ("targeted-wait-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        recovery.mkdir(mode=0o700)
        (recovery / "before.json").write_text(json.dumps({"proof": proof, "queue_ids": ids, "files": files_before}, indent=2))
        print(f"Recovery evidence: {recovery}", flush=True)
        current = next((f for f in api.call("status_downloads") if f["fid"] == args.file_id), None)
        if current is None or current["statusmsg"] != "waiting" or any(current[k] != 0 for k in ("size", "bleft", "speed")):
            raise RuntimeError("Target changed before restart; no write performed")
        api.call("restart_file", write=True, file_id=args.file_id)
        after = api.call("get_queue_data")
        after_ids = {f["fid"] for p in after for f in p["links"]}
        if not set(ids) <= after_ids:
            raise RuntimeError("Queue IDs missing after restart; stop and inspect")
        if any(not (folder / name).is_file() or (folder / name).stat().st_size != size for name, size in files_before.items()):
            raise RuntimeError("Existing package files changed; stop and inspect")
        file_after = next(f for p in after for f in p["links"] if f["fid"] == args.file_id)
        proof.update(applied=True, state_after=file_after["statusmsg"], queue_ids_preserved=True, existing_files_preserved=True)
        (recovery / "proof.json").write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
