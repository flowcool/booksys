"""Selection continuation is PostgreSQL state, independent per queue."""
from pathlib import Path
import tempfile
import unittest
from library.primitives import empty
from tests.postgres_fixture import create_store

class OperationalCursorTests(unittest.TestCase):
    def test_queue_and_discovery_cursors_survive_reopen_and_ignore_legacy_file(self):
        from library.operational_state import OperationalState
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=create_store(self,root,empty())
            state=OperationalState(store._database)
            state.save_selection('intake',('2026-11-10T00:00:00+00:00','task'),True)
            state.save_selection('metadata',None,False)
            state.save_discovery('asset42')
            state.save_discovery('source100',kind='intake')
            state.save_discovery('asset43')
            (root/'stage-selection-intake.yaml').write_text('invalid legacy journal')
            again=OperationalState(store._database)
            self.assertEqual(again.selection('intake'),(('2026-11-10T00:00:00+00:00','task'),True))
            self.assertEqual(again.selection('metadata'),(None,False))
            self.assertEqual(again.discovery(),'asset43')
            self.assertEqual(again.discovery('intake'),'source100')
            again.save_discovery('',kind='intake')
            self.assertEqual(state.discovery(),'asset43')
