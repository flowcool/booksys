"""Start the collector against the existing shared pyLoad. Never create a second downloader."""

import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
assert Path("/data/booksys/secrets/pyload.json").is_file(), "Configure access to the existing pyLoad first"
subprocess.run(["docker", "inspect", "booksys-pyload"], check=True, stdout=subprocess.DEVNULL)
subprocess.run(["docker", "compose", "-f", "compose.runtime.yaml", "up", "-d", "--build"], cwd=root, check=True)
