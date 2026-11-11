"""Replace only the Booksys browser; retain worker identity and current journals."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NAMES = ["booksys-worker-1", "booksys-byparr-1", "booksys-pyload"]


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def inspect() -> list[dict[str, Any]]:
    return json.loads(run("docker", "inspect", *NAMES))  # type: ignore[no-any-return]


def wait_browser(image: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        browser = inspect()[1]
        if browser["Image"] != image:
            raise RuntimeError("Browser image drift")
        if browser["State"].get("Health", {}).get("Status") == "healthy":
            return
        if not browser["State"]["Running"]:
            raise RuntimeError("Browser is not running")
        time.sleep(1)
    raise RuntimeError("Browser readiness timeout; worker remains stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-worker", required=True)
    parser.add_argument("--expected-browser", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--worker-quiesced", action="store_true", help="Keep the already stopped collector stopped")
    args = parser.parse_args()
    os.umask(0o077)
    if run("docker", "info", "--format", "{{.Name}}").strip() != "booksys-host":
        raise RuntimeError("Expected booksys-host")
    candidate = json.loads(run("docker", "image", "inspect", args.image))[0]["Id"]
    before = inspect()
    worker, browser, downloader = before
    if worker["Id"] != args.expected_worker or browser["Id"] != args.expected_browser:
        raise RuntimeError("Container target drift; inspect before retrying")
    if args.worker_quiesced and worker['State']['Running']:
        raise RuntimeError('Expected a quiesced collector')
    if not all(c["State"]["Running"] for c in (before[1:] if args.worker_quiesced else before)):
        raise RuntimeError("All existing services must be running")
    if any(c["Config"]["Labels"].get("com.docker.compose.project") != "booksys" for c in before[:2]):
        raise RuntimeError("Unexpected Compose project")
    mounts = {m["Destination"]: m["Source"] for m in worker["Mounts"]}
    if mounts.get("/state") != "/data/booksys/state":
        raise RuntimeError("Unexpected worker state mount")
    from ops.compose import COMMON, arguments
    arguments()  # Check canonical logging settings and shell overrides.
    compose = json.loads(run("docker", "compose", "--env-file", str(COMMON), "-p", "booksys", "--project-directory",
                            str(args.runtime_root.resolve()), "-f", str(args.runtime_root / "compose.runtime.yaml"),
                            "config", "--format", "json"))
    if compose["services"]["byparr"]["image"] != "booksys-byparr:local":
        raise RuntimeError("Integrate the session runtime configuration before deployment")
    old_image = browser["Image"]
    run("docker", "image", "inspect", old_image)
    print(json.dumps({"worker": worker["Id"], "previous_browser": browser["Id"],
                      "previous_image": old_image, "candidate": candidate}), flush=True)
    if not args.apply:
        return
    state = Path(mounts["/state"])
    with (state / "byparr-maintenance.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if [c["Id"] for c in inspect()] != [c["Id"] for c in before]:
            raise RuntimeError("Container drift before maintenance")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        recovery = Path("/data/booksys/recovery") / ("byparr-session-" + stamp)
        recovery.mkdir(mode=0o700)
        (recovery / "before.json").write_text(json.dumps(before, indent=2))
        previous_tag = "booksys-byparr:before-session-" + stamp.lower()
        run("docker", "image", "tag", old_image, previous_tag)
        service = compose["services"]["byparr"]
        service.pop("build", None)
        service["image"] = old_image
        health = browser["Config"]["Healthcheck"]
        service["healthcheck"] = {"test": health["Test"], "retries": health["Retries"]}
        for source, target in [("Interval", "interval"), ("Timeout", "timeout"),
                               ("StartPeriod", "start_period")]:
            if health.get(source):
                service["healthcheck"][target] = str(health[source]) + "ns"
        (recovery / "rollback.json").write_text(json.dumps(compose, indent=2))
        service["image"] = candidate
        service["healthcheck"]["test"][-1] = "http://127.0.0.1:8191/health"
        (recovery / "candidate.json").write_text(json.dumps(compose, indent=2))
        print("Recovery: " + str(recovery), flush=True)
        if not args.worker_quiesced:
            run("docker", "stop", "--timeout", "30", worker["Id"])
        # No independent YAML restore: the downloader and journals must stay in sync.
        shutil.copytree(state, recovery / "state")
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in state.glob("*.yaml")}
        (recovery / "state-hashes.json").write_text(json.dumps(hashes, indent=2))
        run("docker", "compose", "-p", "booksys", "-f", str(recovery / "candidate.json"),
            "up", "-d", "--no-deps", "--no-build", "--pull", "never", "byparr")
        wait_browser(candidate)
        if any(hashlib.sha256((state / name).read_bytes()).hexdigest() != digest for name, digest in hashes.items()):
            raise RuntimeError("State changed while worker stopped; inspect before resuming")
        after = inspect()
        if after[0]["Id"] != worker["Id"] or after[2]["Id"] != downloader["Id"]:
            raise RuntimeError("Worker or downloader changed; inspect before resuming")
        run("docker", "image", "tag", candidate, "booksys-byparr:local")
        if not args.worker_quiesced:
            run("docker", "start", worker["Id"])
        proof = {"worker_preserved": worker["Id"], "downloader_preserved": downloader["Id"],
                 "candidate_image": candidate, "browser": after[1]["Id"], "journals_unchanged_before_resume": True,
                 "recovery": str(recovery), "previous_tag": previous_tag}
        (recovery / "proof.json").write_text(json.dumps(proof, indent=2))
        print(json.dumps(proof), flush=True)


if __name__ == "__main__":
    main()
