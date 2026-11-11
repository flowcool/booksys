"""Apply authorized retry/logging maintenance; retain private recovery evidence.

On failure, stop and inspect the recovery directory. Do not automatically retry
the operation or resume the collector after an unverified replacement.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import Pyload
from ops.compose import arguments as compose_arguments


def run(*args: str) -> str:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True).stdout


def inspect(name: str) -> dict:
    return json.loads(run("docker", "inspect", name))[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-compose-ref", required=True, help="Verified Git revision of the previous shared deployment")
    args = parser.parse_args()
    os.umask(0o077)
    assert run("docker", "info", "--format", "{{.Name}}").strip() == "booksys-host"
    service = yaml.safe_load((ROOT / "compose.shared.yaml").read_text())["services"]["pyload"]
    current = inspect("booksys-pyload")
    assert current["Image"] == service["image"] and current["State"]["Running"]
    expected_mounts = {"/config": "/data/booksys/config/pyload",
                       "/downloads": "/data/booksys/downloads"}
    mounts = {m["Destination"]: m["Source"] for m in current["Mounts"]}
    assert all(mounts.get(k) == v for k, v in expected_mounts.items())
    print(run("uv", "run", "python", "ops/check_pyload_logging.py"), flush=True)
    key = json.loads(Path("/data/booksys/secrets/pyload.json").read_text())["api_key"]
    api = Pyload("http://127.0.0.1:8000", key)
    desired = [("plugin", "RestartFailed", "interval", 90),
               ("plugin", "RestartFailed", "enabled", True),
               ("core", "general", "debug_level", "debug"),
               ("core", "general", "debug_mode", False)]
    configs = {"core": api.call("get_config"), "plugin": api.call("get_plugin_config")}
    previous = [{"section": section, "category": category, "option": option,
                 "value": next(i["value"] for i in configs[section][category]["items"] if i["name"] == option)}
                for section, category, option, _ in desired]
    previous_compose = run("git", "show", f"{args.previous_compose_ref}:compose.shared.yaml")
    assert yaml.safe_load(previous_compose)["services"]["pyload"]["image"] == current["Image"]
    worker_running = inspect("booksys-worker-1")["State"]["Running"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    recovery = Path("/data/booksys/recovery") / f"pyload-settings-{stamp}"
    recovery.mkdir(mode=0o700)
    snapshot = {"settings": previous, "collector_was_running": worker_running,
                "previous_compose_ref": run("git", "rev-parse", args.previous_compose_ref).strip()}
    (recovery / "before.json").write_text(json.dumps(snapshot, indent=2))
    (recovery / "original-container.json").write_text(json.dumps(current, indent=2))
    (recovery / "compose.shared.yaml").write_text(previous_compose)
    with (recovery / "previous-container.log").open("w") as output:
        subprocess.run(["docker", "logs", "booksys-pyload"], stdout=output, stderr=subprocess.STDOUT, check=True)
    print(f"Recovery evidence: {recovery}", flush=True)
    if worker_running:
        run(*compose_arguments(), "stop", "worker")
    before = {f["fid"] for p in api.call("get_queue_data") for f in p["links"]}
    snapshot["queue_file_ids"] = sorted(before)
    (recovery / "before.json").write_text(json.dumps(snapshot, indent=2))
    for section, category, option, value in desired:
        api.call("set_config_value", write=True, section=section, category=category, option=option, value=value)
        actual = api.call("get_config_value", section=section, category=category, option=option)
        assert str(actual).lower() == str(value).lower(), (category, option, actual)
    print("Native retry/debug settings verified; recreating shared downloader", flush=True)
    run(*compose_arguments(shared=True), "up", "-d", "--force-recreate", "--no-deps", "pyload")
    deadline = time.monotonic() + 90
    while True:
        try:
            api.call("status_server")
            break
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    for section, category, option, value in desired:
        actual = api.call("get_config_value", section=section, category=category, option=option)
        assert str(actual).lower() == str(value).lower(), (category, option, actual)
    after = {f["fid"] for p in api.call("get_queue_data") for f in p["links"]}
    assert before <= after, "Original queue IDs missing; keep collector stopped"
    current = inspect("booksys-pyload")
    assert current["HostConfig"]["LogConfig"] == {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}}
    floor = run("docker", "exec", "booksys-pyload", "python", "-c",
                "import logging, sitecustomize; assert logging.root.manager.disable == logging.INFO; print('WARNING floor active')")
    assert "WARNING floor active" in floor
    proof = {"original_queue_files": len(before), "queue_files_preserved": True,
             "retry_interval_minutes": 90, "retry_enabled": True, "debug_mode": False,
             "logging_floor": "WARNING", "docker_log_rotation": current["HostConfig"]["LogConfig"],
             "status": api.call("status_server")}
    (recovery / "proof.json").write_text(json.dumps(proof, indent=2))
    if worker_running:
        run(*compose_arguments(), "start", "worker")
    print(json.dumps(proof), flush=True)


if __name__ == "__main__":
    main()
