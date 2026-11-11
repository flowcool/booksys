"""Conservative native-writer demand from the retained bounded observation."""
from datetime import datetime
from pathlib import Path
import yaml
from library.primitives import LibraryError
from library.source_tasks import bounded_read


def external_capacity(path: Path, *, now: datetime) -> int | None:
    try:
        from library.operational_state import RuntimeObservation
        from library.postgres import Database
        value = RuntimeObservation(Database.configured(path.parent.parent/'state')).capacity()
        if (value['schema'] != 1 or not 0 <= now.timestamp() - value['observed_at'] <= 90
                or any(type(value[key]) is not int or value[key] < 0 for key in ('remaining_bytes', 'overshoot_bytes'))):
            return None
        return int(value['remaining_bytes'] + value['overshoot_bytes'])
    except (OSError, LibraryError, ValueError, TypeError, KeyError, yaml.YAMLError):
        return None
