"""Historical recovery status; runtime never snapshots or restores SQLite."""
from typing import Any


class RecoverySchedule:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def status(self) -> dict[str,Any]:
        return {'mode':'postgresql-forward-only','requested':False,'overdue':False,'failed':False,
                'last_verified':None,'revision':None,'age_seconds':None}

    def request_boundary(self, *, completed_cycle: bool = False) -> bool:
        return False
