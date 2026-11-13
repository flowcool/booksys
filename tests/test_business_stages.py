"""Separate durable business steps through real catalogue and copy adapters."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.business_stages import BusinessStages
from library.stage_tasks import StageTasks
from library.metadata import Providers
from library.publication import Capacity
from test_library_publish import setup_library
from postgres_fixture import require_postgresql

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)

class BusinessStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog, self.reception, self.source, self.aid = setup_library(self.root,self)
        require_postgresql(self.catalog)
        self.tasks = StageTasks(self.catalog.backend())
        StageTasks.upgrade(self.tasks.store)
        self.stages = BusinessStages(self.catalog, self.reception, self.root/'library',
            Providers(self.root/'cache', fetch=lambda url: (503, ''), interval=0),
            capacity=Capacity(reserve=0), isolated=True)

    def execute(self, task):
        from library.stage_model import StepResult
        active = self.tasks.start(task.key, task.generation, now=AT, resources=True)
        value = self.stages.execute(active, now=AT)
        result = value if isinstance(value, StepResult) else StepResult(value)
        self.tasks.complete(active.key, active.generation, now=AT, outcome=result.outcome,
            proof=result.proof, due_at=result.due_at, prepared=result.prepared,
            successors=result.successors, source_progress=result.source_progress)
        return result
