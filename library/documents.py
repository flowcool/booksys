"""Private YAML inputs shared by adapters and orchestration."""
from pathlib import Path
from typing import Any
import yaml


def load(path: Path) -> Any:
    return yaml.load(path.read_text(), Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
