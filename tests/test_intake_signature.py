"""Intake inputs stay stable across its own saved outcomes."""
from __future__ import annotations

import json
import copy
import io
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

from booksys_core.diagnostics import event
from library.catalog import Catalog
from library.intake import receive
from library.native_stages import NativeStages
from library.primitives import LibraryError, WorkingState, digest, empty
from library.stage_tasks import StageTasks
from library.stage_model import Obligation, Task

SOURCE = 'https://www.bookys-ebooks.com/123-album'
AT = datetime(2026, 11, 16, tzinfo=timezone.utc)


def memory_catalog(root: Path) -> tuple[Any, dict[str, Any]]:
    """Inject storage only when no database is needed by the tested behavior."""
    records = empty()
    catalog = MagicMock(spec=Catalog)
    catalog.directory = root / 'state'
    catalog.revision = 0
    catalog.result_set.side_effect = lambda *args, **kwargs: WorkingState(copy.deepcopy(records))
    catalog.working_set.side_effect = catalog.result_set.side_effect
    def save(state: dict[str, Any], **kwargs: Any) -> None:
        records.clear()
        records.update(copy.deepcopy(state))
    catalog.save.side_effect = save
    return catalog, records


class IntakeSignatureTests(unittest.TestCase):
    def test_real_input_and_policy_changes_change_revision(self) -> None:
        from library.intake import intake_signature
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'unsupported.txt'
            path.write_text('unchanged')
            package: dict[str, Any] = {'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                {'fid': 1, 'name': path.name, 'status': 0, 'size': 9}]}
            first = intake_signature(root, package)
            path.write_text('new bytes with a new fingerprint')
            changed_file = intake_signature(root, package)
            self.assertNotEqual(first, changed_file)
            package['name'] = 'new package'
            changed_package = intake_signature(root, package)
            self.assertNotEqual(changed_file, changed_package)
            package['links'][0]['status'] = 7
            changed_status = intake_signature(root, package)
            self.assertNotEqual(changed_package, changed_status)
            with patch('library.intake.READER_POLICY', 'new-reader'):
                self.assertNotEqual(changed_status, intake_signature(root, package))
            with patch('library.bundle_worker.POLICY', 'new-bundle'):
                self.assertNotEqual(changed_status, intake_signature(root, package))

    def test_discovery_ignores_its_own_pending_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'unsupported.txt').write_text('unchanged')
            catalog = MagicMock(spec=Catalog)
            catalog.directory = root / 'state'
            state = empty()
            catalog.working_set.return_value = state
            tasks = MagicMock(spec=StageTasks)
            stages = NativeStages(catalog, root, root / 'library', root / 'collector',
                                  browser=None, client=None, snapshots=root / 'snapshots')
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                {'fid': 1, 'name': 'unsupported.txt', 'status': 0, 'size': 9}]}]
            with patch('library.native_stages.CollectorState') as collector, \
                    patch('library.native_stages.RuntimeObservation') as observation, \
                    patch('library.native_stages.OperationalState') as cursor, \
                    patch('library.intake.EXTERNAL_ROOT', root / 'absent'):
                collector.return_value.monitor.return_value = {'submissions': {
                    SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
                observation.return_value.queue.return_value = (queue, AT.timestamp())
                cursor.return_value.discovery.return_value = ''
                stages.discover_intake(tasks, now=AT)
                before = tasks.discover.call_args.args[0].input_revision
                oid = digest({'source': SOURCE, 'pid': 1, 'fid': 1})
                state['origins'][oid] = {'source': SOURCE, 'pid': 1, 'fid': 1, 'sha256': None,
                    'availability': 'pending', 'reason': 'Unsupported or ambiguous suffix',
                    'fingerprint': [1, 2, 9, 4, 5], 'intake_attempts': 1, 'bundle_attempts': 0}
                stages.discover_intake(tasks, now=AT)
                self.assertEqual(before, tasks.discover.call_args.args[0].input_revision)
                queue.append({'pid': 2, 'name': 'mirror', 'folder': '.', 'mirror_source': SOURCE,
                              'mirror_original_pid': 1, 'links': [
                                  {'fid': 2, 'name': 'unsupported.txt', 'status': 0, 'size': 9}]})
                stages.discover_intake(tasks, now=AT)
                self.assertNotEqual(before, tasks.discover.call_args.args[0].input_revision)


class IntakeReuseTests(unittest.TestCase):
    def test_nested_bundle_preparation_has_a_finite_retry_round(self) -> None:
        from test_library_bundles import comic
        from library.decode import run
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception = root / 'reception'
            reception.mkdir()
            with zipfile.ZipFile(reception / 'nested.cbz', 'w') as archive:
                archive.writestr('child.cbz', comic())  # type: ignore[no-untyped-call]
            catalog, records = memory_catalog(root)
            captured: dict[str, Any] = {'items': {SOURCE: {}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                {'fid': 1, 'name': 'nested.cbz', 'status': 0, 'size': 123}]}]
            work = root / 'work'
            work.mkdir()
            extractions = 0
            def decode(operation: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
                nonlocal extractions
                if operation == 'bundle':
                    extractions += 1
                    return {'status': 'pending', 'reason': 'TimeoutError: timed out'}
                return run(operation, *args, **kwargs)
            with patch('library.decode.WORK_ROOT', str(work)), patch('library.decode.run', side_effect=decode), \
                    patch('library.intake.time.time') as clock:
                for seconds in (0, 1, 300, 1200, 2400):
                    clock.return_value = AT.timestamp() + seconds
                    receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
            self.assertEqual(extractions, 3)
            origin = next(iter(records['origins'].values()))
            self.assertEqual(origin['bundle_attempts'], 3)

    def test_bundle_child_retries_are_bounded_and_decoder_recovery_starts_a_new_round(self) -> None:
        from test_library_bundles import inputs
        from library.decode import run
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            child_bytes = io.BytesIO()
            with zipfile.ZipFile(child_bytes, 'w') as archive:
                archive.writestr('001.png', b'content is never decoded')
            reception, _, captured, journal, queue = inputs(root, [('child.cbz', child_bytes.getvalue())])  # type: ignore[no-untyped-call]
            catalog, records = memory_catalog(root)
            work = root / 'work'
            work.mkdir()
            tools = root / 'bin'
            tools.mkdir()
            validations = 0
            def decode(operation: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
                nonlocal validations
                if operation == 'validate':
                    validations += 1
                    raise LibraryError('Decoder timed out')
                return run(operation, *args, **kwargs)
            with patch.dict(os.environ, {'PATH': str(tools)}), patch('library.decode.WORK_ROOT', str(work)), \
                    patch('library.decode.run', side_effect=decode), patch('library.intake.time.time') as clock:
                for seconds in (0, 300, 600, 900):
                    clock.return_value = AT.timestamp() + seconds
                    receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
                self.assertEqual(validations, 3)
                child = next(row for row in records['origins'].values() if row.get('parent_origin'))
                self.assertEqual(child['intake_attempts'], 3)
                decoder = tools / 'unrar'
                decoder.write_text('#!/bin/sh\nexit 0\n')
                decoder.chmod(0o700)
                receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
                self.assertEqual(validations, 4)
                child = next(row for row in records['origins'].values() if row.get('parent_origin'))
                self.assertEqual(child['intake_attempts'], 1)

    def test_decoder_readiness_change_reopens_parked_input(self) -> None:
        from library.intake import intake_signature
        from library.readers import inspect_file
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception = root / 'reception'
            reception.mkdir()
            (reception / 'unsupported.txt').write_text('unchanged')
            tools = root / 'bin'
            tools.mkdir()
            catalog, _ = memory_catalog(root)
            captured: dict[str, Any] = {'items': {SOURCE: {}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                {'fid': 1, 'name': 'unsupported.txt', 'status': 0, 'size': 9}]}]
            with patch.dict(os.environ, {'PATH': str(tools)}), patch('library.intake.inspect_file', wraps=inspect_file) as reader:
                before = intake_signature(reception, queue[0])
                receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
                decoder = tools / 'unrar'
                decoder.write_text('#!/bin/sh\nexit 0\n')
                decoder.chmod(0o700)
                self.assertNotEqual(before, intake_signature(reception, queue[0]))
                receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
            self.assertEqual(reader.call_count, 2)

    def test_unchanged_pending_bundle_child_is_parked(self) -> None:
        from test_library_bundles import inputs
        from test_library_readers import image_bytes
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            child_bytes = io.BytesIO()
            with zipfile.ZipFile(child_bytes, 'w') as archive:
                for name in ('album-a/001.png', 'album-b/001.png'):
                    archive.writestr(name, image_bytes())  # type: ignore[no-untyped-call]
            reception, _, captured, journal, queue = inputs(root, [('ambiguous.cbz', child_bytes.getvalue())])  # type: ignore[no-untyped-call]
            catalog, records = memory_catalog(root)
            work = root / 'work'
            work.mkdir()
            with patch('library.decode.WORK_ROOT', str(work)):
                for _ in range(2):
                    receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
            child = next(row for row in records['origins'].values() if row.get('parent_origin'))
            self.assertEqual(child['availability'], 'pending')
            self.assertEqual(child['intake_attempts'], 1)

    def test_due_transient_retries_run_without_changing_the_signature(self) -> None:
        from library.intake import intake_signature
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception = root / 'reception'
            reception.mkdir()
            (reception / 'album.cbz').write_bytes(b'PK\x03\x04not a real archive')
            catalog, records = memory_catalog(root)
            stages = NativeStages(catalog, reception, root / 'library', root / 'collector',
                                  browser=None, client=None, snapshots=root / 'snapshots')
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                {'fid': 1, 'name': 'album.cbz', 'status': 0, 'size': 23}]}]
            signature = intake_signature(reception, queue[0])
            obligation = Obligation('intake', 'source-' + digest(SOURCE), signature, 'intake-step-v1')
            task = Task(obligation.key, 'intake', obligation.entity, signature, obligation.method, None, AT.isoformat())
            with patch('library.native_stages.CollectorState') as collector, \
                    patch('library.native_stages.RuntimeObservation') as observation, \
                    patch('library.decode.run', side_effect=LibraryError('Decoder timed out')) as decoder, \
                    patch('library.intake.time.time', return_value=AT.timestamp()) as clock, \
                    patch('library.readers.time.sleep'):
                collector.return_value.monitor.return_value = {'submissions': {
                    SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
                collector.return_value.captured.return_value = {'items': {SOURCE: {}}}
                observation.return_value.queue.return_value = (queue, AT.timestamp())
                for seconds, expected_calls, outcome, next_seconds in (
                        (0, 1, 'defer', 300), (100, 1, 'defer', 300),
                        (300, 2, 'defer', 1200), (1200, 3, 'done', None), (9999, 3, 'done', None)):
                    clock.return_value = AT.timestamp() + seconds
                    result = stages.execute(task, now=datetime.fromtimestamp(clock.return_value, timezone.utc))
                    self.assertEqual(result.outcome, outcome)
                    self.assertEqual(decoder.call_count, expected_calls)
                    self.assertEqual(result.due_at.timestamp() if result.due_at else None,
                                     AT.timestamp() + next_seconds if next_seconds else None)
                    self.assertEqual(intake_signature(reception, queue[0]), signature)
                self.assertEqual(next(iter(records['origins'].values()))['intake_attempts'], 3)

    def test_retained_bundle_and_rejected_child_do_not_spend_attempts_on_reuse(self) -> None:
        from test_library_bundles import inputs
        from library.intake import intake_signature
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            child_bytes = io.BytesIO()
            with zipfile.ZipFile(child_bytes, 'w') as archive:
                archive.writestr('001.png', b'corrupt image')
            reception, _, captured, journal, queue = inputs(root, [('bad.cbz', child_bytes.getvalue())])  # type: ignore[no-untyped-call]
            catalog, records = memory_catalog(root)
            work = root / 'work'
            work.mkdir()
            before = intake_signature(reception, queue[0])
            with patch('library.decode.WORK_ROOT', str(work)):
                receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
                first = copy.deepcopy(records['origins'])
                receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
            self.assertEqual(before, intake_signature(reception, queue[0]))
            parent = next(row for row in first.values() if row.get('bundle'))
            self.assertEqual(parent['bundle_attempts'], 1)
            child_id = next(oid for oid, row in first.items() if row.get('parent_origin'))
            self.assertEqual(first[child_id]['availability'], 'rejected')
            self.assertEqual(records['origins'][child_id]['intake_attempts'], 1)
            current_parent = next(row for row in records['origins'].values() if row.get('bundle'))
            self.assertEqual(current_parent['bundle_attempts'], 1)

    def test_unchanged_unsupported_input_is_parked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception = root / 'reception'
            reception.mkdir()
            (reception / 'unsupported.txt').write_text('unchanged')
            catalog, records = memory_catalog(root)
            captured: dict[str, Any] = {'items': {SOURCE: {}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                {'fid': 1, 'name': 'unsupported.txt', 'status': 0, 'size': 9}]}]
            from library.readers import inspect_file
            with patch('library.intake.inspect_file', wraps=inspect_file) as reader:
                for _ in range(2):
                    receive(catalog, reception, captured, journal, queue, incremental=True,
                            stable_seconds=0)
            self.assertEqual(reader.call_count, 1)
            origin = next(iter(records['origins'].values()))
            self.assertEqual(origin['availability'], 'pending')
            self.assertEqual(origin['intake_attempts'], 1)

    def test_reader_policy_change_reinspects_parked_input(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception = root / 'reception'
            reception.mkdir()
            (reception / 'unsupported.txt').write_text('unchanged')
            catalog, _ = memory_catalog(root)
            captured: dict[str, Any] = {'items': {SOURCE: {}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                {'fid': 1, 'name': 'unsupported.txt', 'status': 0, 'size': 9}]}]
            from library.readers import inspect_file
            with patch('library.intake.inspect_file', wraps=inspect_file) as reader:
                receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
                with patch('library.intake.READER_POLICY', 'new-reader'), patch('library.readers.READER_POLICY', 'new-reader'):
                    receive(catalog, reception, captured, journal, queue, incremental=True, stable_seconds=0)
            self.assertEqual(reader.call_count, 2)


class IntakeSaveTests(unittest.TestCase):
    def test_save_failure_reports_inner_cause_once_and_keeps_boundary(self) -> None:
        for phase in ('write', 'refresh'):
            for cause in (LibraryError('storage_busy'), OSError('secret=hidden https://private.invalid')):
                with self.subTest(phase=phase, cause=type(cause).__name__), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    (root / 'unsupported.txt').write_text('unchanged')
                    # Storage is the injected boundary. Parsing and diagnostics stay real.
                    catalog = MagicMock(spec=Catalog)
                    catalog.directory = root / 'state'
                    state = WorkingState(empty())
                    catalog.result_set.side_effect = [state, cause] if phase == 'refresh' else [state]
                    if phase == 'write':
                        catalog.save.side_effect = cause
                    captured: dict[str, Any] = {'items': {SOURCE: {}}}
                    journal = {'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
                    queue = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                        {'fid': 1, 'name': 'unsupported.txt', 'status': 0, 'size': 9}]}]
                    events: list[dict[str, Any]] = []
                    with patch('booksys_core.diagnostics.emit', side_effect=lambda **fields: events.append(event(**fields))):
                        with self.assertRaisesRegex(RuntimeError, '^Intake save failed$') as caught:
                            receive(catalog, root, captured, journal, queue, stable_seconds=0)
                    self.assertIs(caught.exception.__cause__, cause)
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]['exception_type'], type(cause).__name__)
                    self.assertEqual(events[0]['reason'], 'storage_busy' if isinstance(cause, LibraryError) else 'unexpected_exception')
                    self.assertTrue(any(frame['module'] == 'library.intake' and frame['function'] == 'save'
                                        for frame in events[0]['stack']))
                    self.assertNotIn('private.invalid', json.dumps(events))
                    self.assertNotIn('hidden', json.dumps(events))
                    self.assertNotEqual(next(iter(state['origins'].values()))['availability'], 'rejected')


class IntakeQueueTests(unittest.TestCase):
    def test_discovery_preserves_the_active_claim_and_done_until_inputs_change(self) -> None:
        from postgres_fixture import create_catalog
        from library.operational_state import CollectorState, RuntimeObservation
        from library.work_actor import WorkActor
        from library.work_items import WorkItems
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'BOOKSYS_WORK_STAGES': 'intake'}):
            root = Path(folder)
            reception = root / 'reception'
            reception.mkdir()
            path = reception / 'unsupported.txt'
            path.write_text('unchanged')
            catalog: Catalog = create_catalog(self, root / 'state')  # type: ignore[no-untyped-call]
            backend = catalog.backend()
            tasks = StageTasks(backend)
            StageTasks.upgrade(backend)
            work = WorkItems(backend._database, max_attempts={'intake': 3})
            self.addCleanup(work.close)
            collector = CollectorState(root / 'collector', database=backend._database)
            entry = {'status': 'queued', 'package_id': 1, 'name': 'tracked'}
            collector.submission(SOURCE, entry)
            collector.capture(SOURCE, {'title': 'Album'})
            queue: list[dict[str, Any]] = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                {'fid': 1, 'name': path.name, 'status': 0, 'size': 9}]}]
            observation = RuntimeObservation(backend._database)
            observation.publish(queue, at=AT.timestamp(), remaining=None, overshoot=None)
            stages = NativeStages(catalog, reception, root / 'library', root / 'collector',
                                  browser=None, client=None, snapshots=root / 'snapshots')
            actor = WorkActor(backend, work, 'intake', lambda task, now: stages.execute(task, now=now))
            original_save = catalog.save
            def save_and_discover(state: dict[str, Any], *, expected: int) -> None:
                original_save(state, expected=expected)
                stages.discover_intake(tasks, now=AT)
            with patch('library.intake.EXTERNAL_ROOT', root / 'absent'), patch.object(catalog, 'save', side_effect=save_and_discover):
                stages.discover_intake(tasks, now=AT)
                self.assertEqual(actor.step(now=AT), 'done')
                for _ in range(2):
                    stages.discover_intake(tasks, now=AT)
                self.assertEqual(actor.step(now=AT), 'idle')
                self.assertEqual(work.counts('intake'), {'intake': {'done': 1}})
                path.write_text('a new fingerprint')
                stages.discover_intake(tasks, now=AT)
                self.assertEqual(work.counts('intake'), {'intake': {'ready': 1}})
                self.assertEqual(actor.step(now=AT), 'done')
                queue[0]['name'] = entry['name'] = 'new package'
                collector.submission(SOURCE, entry)
                observation.publish(queue, at=AT.timestamp() + 1, remaining=None, overshoot=None)
                stages.discover_intake(tasks, now=AT)
                self.assertEqual(actor.step(now=AT), 'done')
                with patch('library.intake.READER_POLICY', 'new-reader'), patch('library.readers.READER_POLICY', 'new-reader'):
                    stages.discover_intake(tasks, now=AT)
                    self.assertEqual(actor.step(now=AT), 'done')
