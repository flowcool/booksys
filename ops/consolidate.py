"""Consolidate into the existing pyLoad after the explicit singleton instruction.

Keep the stopped original container until queue and byte-copy proof passes.
Keep the old download directory and consistent config backup for recovery.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = Path("/data/booksys")
LEGACY = Path("/opt/related-project")
IMAGE = "sha256:222f1486954e052f61d3361ab2ad5e97e17f698d65c3a199e4689a3268a68a64"


def run(*args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True)
    if args[:2] != ("docker", "inspect"):
        print(result.stdout[-2000:], result.stderr[-2000:], flush=True)
    return result.stdout


def snapshot(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    result = {table: list(connection.execute(f"SELECT * FROM {table} ORDER BY id"))
              for table in ["packages", "links", "users"]}
    connection.close()
    return result


def main():
    os.umask(0o077)
    assert run("docker", "info", "--format", "{{.Name}}").strip() == "booksys-host"
    original = json.loads(run("docker", "inspect", "booksys-pyload"))[0]
    assert original["Image"] == IMAGE
    assert {m["Destination"]: m["Source"] for m in original["Mounts"]} == {
        "/config": str(LEGACY / ".pyload/config"), "/downloads": str(LEGACY / "downloads")}
    assert shutil.disk_usage(DATA).free > 40 * 1024**3
    recovery = DATA / "recovery/singleton"
    assert not recovery.exists(), "Inspect the existing recovery bundle before resuming"
    recovery.mkdir(parents=True)
    (recovery / "original-inspect.json").write_text(json.dumps(original, indent=2))
    run("docker", "compose", "-f", "compose.runtime.yaml", "stop", "worker")
    run("docker", "stop", "booksys-pyload-1")
    duplicate = DATA / "pyload/data/pyload.db"
    assert not snapshot(duplicate)["packages"], "Duplicate downloader has packages; reconcile before retirement"
    run("docker", "stop", "booksys-pyload")
    database = LEGACY / ".pyload/config/data/pyload.db"
    before = snapshot(database)
    shutil.copytree(LEGACY / ".pyload/config", recovery / "config")
    shutil.copy2(DATA / "secrets/pyload.json", recovery / "dedicated-credentials.json")
    config_path = LEGACY / ".pyload/config/settings/pyload.cfg"
    config_bytes, changes = re.subn(rb"(?m)^([ \t]*int[ \t]+min_free_space[^\r\n=]*=[ \t]*)\d+",
                                   rb"\g<1>20480", config_path.read_bytes())
    assert changes == 1, "Expected one disk reserve setting"
    config_path.write_bytes(config_bytes)
    run("rsync", "-a", "--checksum", str(LEGACY / "downloads") + "/", str(DATA / "downloads") + "/")
    differences = run("rsync", "-anc", "--itemize-changes", str(LEGACY / "downloads") + "/", str(DATA / "downloads") + "/")
    assert not differences.strip(), "Copied files differ"
    run("docker", "run", "--rm", "--network", "none", "--entrypoint", "python", "--user", "1000:1000",
        "-v", f"{LEGACY / '.pyload/config'}:/config", "-v", f"{DATA / 'secrets'}:/secrets",
        "-v", f"{ROOT / 'ops/authorize_shared_pyload.py'}:/authorize.py:ro", IMAGE, "/authorize.py")
    assert snapshot(database) == before, "Package, link or user records changed during key creation"
    run("docker", "rename", "booksys-pyload", "booksys-original-pyload-recovery")
    run("docker", "compose", "-f", "compose.shared.yaml", "up", "-d")
    from pipeline import Pyload
    credentials = json.loads((DATA / "secrets/pyload.json").read_text())
    client = Pyload("http://127.0.0.1:8000", credentials["api_key"])
    for _ in range(30):
        try:
            status = client.call("status_server")
            break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError("Shared pyLoad API did not start; retain recovery container")
    packages = client.call("get_queue") + client.call("get_collector")
    assert str(client.call("get_config_value", category="general", option="min_free_space")) == "20480"
    assert sorted(p["pid"] for p in packages) == sorted(p[0] for p in before["packages"])
    for retired in ["booksys-pyload-1", "booksys-original-pyload-recovery"]:
        run("docker", "rm", retired)
    proof = {"package_count": len(before["packages"]), "link_count": len(before["links"]),
             "config_backup": str(recovery / "config"), "downloads_verified_with_checksum": True,
             "status": status, "original_downloads_retained": str(LEGACY / "downloads")}
    (ROOT / "tmp/evidence/consolidation/proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    print("Consolidation verified; collector remains stopped until its update is applied", flush=True)


if __name__ == "__main__":
    main()
