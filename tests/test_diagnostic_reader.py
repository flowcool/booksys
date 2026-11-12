"""Bounded technical search uses trusted ingestion time and signed continuations."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from booksys_core.diagnostics import event
from library.diagnostic_reader import DiagnosticReader, MemorySource, Filter


class ReaderTests(unittest.TestCase):
    def test_selective_scan_advances_and_rotation_is_an_explicit_gap(self):
        stamp = datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp()
        rows = []
        for index in range(120):
            value = event(service='collector', stage='byparr', code='attempt', reason='none')
            rows.append((stamp + index, value))
        with tempfile.TemporaryDirectory() as folder:
            source = MemorySource('worker', 'generation', rows)
            reader = DiagnosticReader([source], Path(folder) / 'reader.lock', clock=lambda: stamp + 200, line_limit=20)
            query = Filter(start=stamp, end=stamp + 200, reason='remote_rate_limited')
            page = reader.query(query)
            self.assertEqual(page['state'], 'continued')
            self.assertEqual(page['events'], [])
            next_page = reader.query(query, cursor=page['cursor'])
            self.assertNotEqual(page['cursor'], next_page['cursor'])
            source.generation = 'recreated'
            changed = reader.query(query, cursor=next_page['cursor'])
            self.assertEqual(changed['state'], 'partial')
            self.assertEqual(changed['gaps'][0]['reason'], 'generation_changed')

    def test_pages_merge_sources_without_losing_buffered_events(self):
        stamp = datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp()
        sources = [MemorySource(str(index), 'generation', [(stamp + n,
            event(service='library', stage='local', code='progress', reason='none')) for n in range(index, 150, 3)])
            for index in range(3)]
        with tempfile.TemporaryDirectory() as folder:
            reader = DiagnosticReader(sources, Path(folder) / 'reader.lock', clock=lambda: stamp + 200, line_limit=21)
            query, cursor, events = Filter(start=stamp, end=stamp + 200), None, []
            for _ in range(40):
                page = reader.query(query, cursor=cursor)
                events.extend(page['events'])
                cursor = page['cursor']
                if page['state'] == 'complete':
                    break
            self.assertEqual(len(events), 150)
            self.assertEqual(len({e['event_id'] for e in events}), 150)
            self.assertEqual([e['ingested_at'] for e in events], sorted(e['ingested_at'] for e in events))
            with self.assertRaises(ValueError):
                reader.query(query, cursor='invalid')

    def test_duplicate_continuation_is_replayable_for_several_viewers(self):
        stamp = datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp()
        source = MemorySource('worker', 'one', [(stamp + n, event(service='library',
            stage='local', code='progress', reason='none')) for n in range(100)])
        with tempfile.TemporaryDirectory() as folder:
            reader = DiagnosticReader([source], Path(folder) / 'reader.lock', clock=lambda: stamp + 200, line_limit=20)
            query = Filter(start=stamp, end=stamp + 200)
            first = reader.query(query)
            second = reader.query(query, cursor=first['cursor'])
            self.assertEqual(reader.query(query, cursor=first['cursor']), second)


class RemoteBudgetTests(unittest.TestCase):
    def test_slow_fixed_sources_continue_without_splitting_the_remaining_budget(self):
        import time
        from unittest.mock import patch
        clock=[10.]
        class BoundedSource(MemorySource):
            def scan(self, *args, deadline, **kwargs):
                # A real retained source can need 150 ms for inspect plus logs.
                remaining=deadline-time.monotonic()
                if remaining < .13:
                    clock[0]+=max(0,remaining)
                    return {'rows':[],'position':None,'frontier':100,'done':False,'bytes':0,'lines':0,'gaps':[]}
                clock[0]+=.13
                return super().scan(*args, deadline=deadline, **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            sources=[BoundedSource(str(n),'one',[]) for n in range(3)]
            reader=DiagnosticReader(sources,Path(folder)/'lock',clock=lambda:200)
            with patch('time.monotonic',side_effect=lambda:clock[0]):
                first=reader.query(Filter(100,200))
            self.assertEqual(first['gaps'],[])
            self.assertEqual(first['state'],'continued')
            page=first
            for _ in range(3):
                with patch('time.monotonic',side_effect=lambda:clock[0]):
                    page=reader.query(Filter(100,200),cursor=page['cursor'])
                if page['state']=='complete':break
            self.assertEqual(page['state'],'complete')

    def test_remote_unavailable_is_reported_as_a_source_gap(self):
        from unittest.mock import patch
        from library.compose_diagnostics import RemoteSource
        from library.reader_inputs import ReaderUnavailable
        with tempfile.TemporaryDirectory() as folder:
            source=RemoteSource('pyload',Path(folder))
            with patch('library.postgres.Database.configured'), patch('library.operational_state.ComposeDiagnostics.retain'), patch(
                    'library.compose_diagnostics.request',side_effect=ReaderUnavailable('bounded refusal')):
                page=DiagnosticReader([source],Path(folder)/'lock',clock=lambda:200).query(Filter(100,200))
            self.assertEqual(page['state'],'partial')
            self.assertEqual(page['gaps'],[{'source':'pyload','reason':'unavailable'}])

    def test_expired_remote_budget_never_retains_or_sends_a_request(self):
        import time
        from unittest.mock import patch
        from library.compose_diagnostics import RemoteSource
        with patch('library.operational_state.ComposeDiagnostics.retain') as retain, patch(
                'library.compose_diagnostics.request') as request:
            with self.assertRaises(TimeoutError):
                RemoteSource('pyload',Path('/unused')).scan(100,200,None,lines=1,byte_limit=100,
                    deadline=time.monotonic()-1)
        retain.assert_not_called()
        request.assert_not_called()

    def test_repeated_timeout_does_not_starve_a_healthy_fixed_source(self):
        class Unavailable(MemorySource):
            def scan(self,*args,**kwargs):raise TimeoutError('bounded timeout')
        with tempfile.TemporaryDirectory() as folder:
            row=event(service='library',stage='local',code='progress',reason='none')
            sources=[Unavailable('first','one',[]),MemorySource('second','one',[(150,row)])]
            reader=DiagnosticReader(sources,Path(folder)/'lock',clock=lambda:200)
            page=reader.query(Filter(100,200))
            state=next(iter(reader.cache.values()))
            self.assertEqual(state['positions']['second'],1)
            self.assertIsNone(state['positions']['first'])
            self.assertEqual(page['state'],'continued')
            self.assertEqual(page['gaps'],[])
            # Global ordering keeps healthy-source events buffered until the
            # preceding source progresses. No retained position is discarded.
            self.assertEqual([v['event_id'] for v in state['pending']],[row['event_id']])

    def test_ipc_deadline_preserves_source_position_for_continuation(self):
        from unittest.mock import patch
        from library.compose_diagnostics import RemoteSource
        from library.reader_inputs import ReaderUnavailable
        failure=ReaderUnavailable('Socket deadline')
        failure.__cause__=TimeoutError('deadline')
        with tempfile.TemporaryDirectory() as folder:
            source=RemoteSource('pyload',Path(folder))
            with patch('library.postgres.Database.configured'), patch('library.operational_state.ComposeDiagnostics.retain'), patch(
                    'library.compose_diagnostics.request',side_effect=failure):
                page=DiagnosticReader([source],Path(folder)/'lock',clock=lambda:200).query(Filter(100,200))
            self.assertEqual(page['state'],'continued')
            self.assertEqual(page['gaps'],[])
            self.assertIsNotNone(page['cursor'])
