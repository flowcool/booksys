"""Deploy the explicitly authorized local Booksys pipeline on booksys-host.

No remote repository. No change to the existing booksys-pyload container.
Recovery: docker compose -f compose.runtime.yaml stop worker byparr pyload
Keep /data/booksys and tmp/evidence for recovery and proof.
"""

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = Path("/data/booksys")
IMAGE = "lscr.io/linuxserver/pyload-ng@sha256:222f1486954e052f61d3361ab2ad5e97e17f698d65c3a199e4689a3268a68a64"


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    os.umask(0o077)
    host = subprocess.check_output(["docker", "info", "--format", "{{.Name}}"], text=True).strip()
    assert host == "booksys-host", f"Unexpected Docker target: {host}"
    assert shutil.disk_usage("/data").free > 30 * 1024**3
    assert not DATA.exists(), "Existing state needs an explicit inspection; bootstrap is one-shot"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8001))
    baseline = json.loads((ROOT / "tmp/evidence/baseline.json").read_text())
    for part in ["state", "pyload", "downloads", "secrets"]:
        (DATA / part).mkdir(parents=True, mode=0o700)
    journal = {"version": 1, "categories": {baseline["category"]: {
        "after_id": baseline["after_id"], "initialized_at": baseline["initialized_at"]}},
        "submissions": {}}
    (DATA / "state/monitor.yaml").write_text(yaml.safe_dump(journal, sort_keys=False))
    run("docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
        "--user", "1000:1000", "--volume", f"{DATA / 'pyload'}:/config",
        "--volume", f"{DATA / 'secrets'}:/secrets", "--volume",
        f"{ROOT / 'ops/bootstrap_pyload.py'}:/bootstrap.py:ro", IMAGE, "/bootstrap.py")
    run("docker", "compose", "-f", "compose.runtime.yaml", "config", "--quiet")
    run("docker", "compose", "-f", "compose.runtime.yaml", "build", "worker")
    run("docker", "compose", "-f", "compose.runtime.yaml", "up", "-d")


if __name__ == "__main__":
    main()
