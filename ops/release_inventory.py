"""Run inside an exact image, with no network, to record installed components."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

packages = subprocess.run(
    ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n"],
    check=True, capture_output=True, text=True,
).stdout.splitlines()
components = []
for line in packages:
    binary, version, source, source_version = line.split("\t")
    components.append({"id": "dpkg:" + binary, "version": version,
                       "source": source, "source_version": source_version})
for distribution in importlib.metadata.distributions():
    components.append({"id": "python:" + distribution.metadata["Name"].lower(),
                       "version": distribution.version})
components.append({"id": "interpreter:python", "version": sys.version.split()[0]})
# Browser archives are outside dpkg. Record their exact binary/config identities.
for root in (Path("/app"), Path("/root/.cache"), Path("/home"), Path("/opt"), Path("/cache")):
    if root.exists():
        for name in ("application.ini", "platform.ini", "version.json", "seal.json"):
            for path in root.rglob(name):
                try:
                    components.append({"id": "file:" + str(path),
                                       "version": hashlib.sha256(path.read_bytes()).hexdigest()})
                except PermissionError:
                    raise SystemExit("Inventory must run as root to read browser metadata")
print(json.dumps(sorted(components, key=lambda entry: entry["id"]), sort_keys=True, indent=2))
