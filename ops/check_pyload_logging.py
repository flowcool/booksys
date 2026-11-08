"""Isolated startup probe: the pinned pyLoad logger must emit WARNING and above."""

import subprocess
from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[1]
service = yaml.safe_load((root / "compose.shared.yaml").read_text())["services"]["pyload"]
probe = """
import json
from pyload.core.log_factory import LogFactory
class Config:
    def get(self, section, option):
        return option == 'console'
class Core:
    config = Config()
    debug = False
    _ = staticmethod(lambda text: text)
logger = LogFactory(Core()).init_logger('pyload')
logger.debug('debug-marker')
logger.info('info-marker')
logger.warning('warning-marker')
logger.error('error-marker')
"""
command = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python"]
for key, value in service.get("environment", {}).items():
    if key == "PYTHONPATH":
        command += ["-e", f"{key}={value}"]
for mount in service["volumes"]:
    if ":/opt/booksys-python:" in mount:
        source, target, mode = mount.split(":")
        command += ["-v", f"{(root / source).resolve()}:{target}:{mode}"]
command += [service["image"], "-c", probe]
result = subprocess.run(command, capture_output=True, text=True, check=True)
output = result.stdout + result.stderr
assert "warning-marker" in output and "error-marker" in output, output
assert "debug-marker" not in output and "info-marker" not in output, output
print("PASS: pinned pyLoad emits WARNING/ERROR and suppresses DEBUG/INFO")
