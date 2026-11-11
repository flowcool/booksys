"""Compose entry point using canonical infrastructure log limits."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess

COMMON = Path('/opt/related-project/repo/env/.env.common')
ROOT = Path(__file__).resolve().parents[1]


def arguments(*, shared: bool = False) -> list[str]:
    if not COMMON.is_file():
        raise RuntimeError('Canonical common environment is missing')
    values = {}
    for line in COMMON.read_text().splitlines():
        if line.startswith(('DOCKER_LOGGING_MAX_FILE=', 'DOCKER_LOGGING_MAX_SIZE=')):
            key, value = line.split('=', 1)
            values[key] = value
    if values != {'DOCKER_LOGGING_MAX_FILE': '3', 'DOCKER_LOGGING_MAX_SIZE': '10m'}:
        raise RuntimeError('Canonical logging limits changed; qualify before deployment')
    # Shell environment takes precedence over --env-file. Do not permit a hidden override.
    if any(key in os.environ and os.environ[key] != value for key, value in values.items()):
        raise RuntimeError('Shell logging override differs from canonical settings')
    return ['docker', 'compose', '--env-file', str(COMMON), '-f',
            str(ROOT / ('compose.shared.yaml' if shared else 'compose.runtime.yaml'))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shared', action='store_true')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    options = parser.parse_args()
    if not options.arguments:
        parser.error('Compose arguments are required')
    raise SystemExit(subprocess.run(arguments(shared=options.shared) + options.arguments, cwd=ROOT).returncode)


if __name__ == '__main__':
    main()
