"""Filtered emission and blocked-sink seam. No real log readers."""
import json
import os
import time
import unittest

from booksys_core.diagnostics import Emitter, event, parse_event


class DiagnosticTests(unittest.TestCase):
    def test_typed_roundtrip_filters_titles_and_rejects_unknown_fields(self):
        value = event(service='library', stage='local', code='failed', reason='invalid_archive',
                      title='Album https://private.example/token password=CANARY\n\x1b[31m',
                      refs=[{'kind': 'asset', 'id': 'a' * 64}])
        parsed = parse_event(json.dumps(value))
        self.assertNotIn('CANARY', parsed['title'])
        self.assertNotIn('private.example', parsed['title'])
        self.assertNotIn('\x1b', parsed['title'])
        for fields in ({'raw': 'CANARY'}, {'exception_type': 'CANARY'},
                       {'refs': [{'kind': 'source', 'id': 'https://private.example'}]}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                parse_event(json.dumps({**value, **fields}))

    def test_public_relay_preserves_child_identity_and_registers_drain(self):
        from unittest.mock import patch, Mock
        from booksys_core import diagnostics
        child = event(service='library', stage='local', code='progress', reason='none')
        sink = Mock()
        with patch.object(diagnostics, '_default', None), patch.object(diagnostics, 'Emitter', return_value=sink), \
             patch('atexit.register') as registration:
            self.assertTrue(diagnostics.relay(child))
            sink.emit.assert_called_once_with(child)
            registration.assert_called_once_with(sink.close)

    def test_full_pipe_and_queue_never_delay_business_and_account_for_loss(self):
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        try:
            while True:
                try:
                    os.write(write_fd, b'x' * 4096)
                except BlockingIOError:
                    break
            emitter = Emitter(write_fd)
            started = time.monotonic()
            for _ in range(600):
                emitter.emit(event(service='library', stage='diagnostics', code='progress', reason='none'))
            emitter.close()
            self.assertLess(time.monotonic() - started, 1)
            self.assertGreater(emitter.degradation()['dropped_count'], 0)
        finally:
            os.close(write_fd)
            os.close(read_fd)

class LoggingTests(unittest.TestCase):
    def test_third_party_messages_never_cross_the_structured_boundary(self):
        import logging
        from unittest.mock import patch
        from booksys_core.diagnostics import SafeLoggingHandler
        record = logging.LogRecord('urllib', logging.ERROR, '/private/CANARY-path', 10,
            'https://host/private?token=CANARY-message %s', ('CANARY-argument',), None)
        with patch('booksys_core.diagnostics.emit') as emitted:
            SafeLoggingHandler('collector').emit(record)
        saved = str(emitted.call_args)
        self.assertNotIn('CANARY', saved)
        self.assertIn('diagnostic_unavailable', saved)

class ErrorDetailTests(unittest.TestCase):
    def test_unexpected_library_error_has_sanitized_message_and_cause(self):
        from booksys_core.diagnostics import exception_details
        from booksys_core.errors import LibraryError
        try:
            try:
                raise RuntimeError('cause token=CAUSE-CANARY')
            except RuntimeError as cause:
                raise LibraryError(
                    "decode /data/books/input.cbz '/home/PRIVATE/input.cbz' "
                    'https://user:URL-CANARY@host/archive token=TOKEN-CANARY '
                    'Authorization: Bearer AUTH-CANARY') from cause
        except LibraryError as exc:
            parsed = parse_event(json.dumps(event(service='library', stage='local',
                code='failed', **exception_details(exc))))
        self.assertEqual(parsed['reason'], 'unexpected_exception')
        self.assertEqual(parsed['exception_type'], 'LibraryError')
        self.assertEqual(parsed['cause_type'], 'RuntimeError')
        self.assertIn('decode /data/books/input.cbz', parsed['message'])
        self.assertNotIn('CANARY', json.dumps(parsed))
        self.assertNotIn('/home/', parsed['message'])
        self.assertNotIn('PRIVATE', parsed['message'])
        self.assertLessEqual(len(parsed['message']), 300)
        self.assertEqual(parsed['schema'], 1)

    def test_plain_exception_message_is_bounded_and_schema_rejects_unsafe_fields(self):
        from booksys_core.diagnostics import exception_details
        parsed = parse_event(json.dumps(event(service='library', stage='local', code='failed',
            **exception_details(Exception('decoder failed: ' + 'x' * 400)))))
        self.assertEqual(parsed['message'], 'decoder failed: ' + 'x' * 284)
        self.assertIsNone(parsed['cause_type'])
        for fields in ({'message': 'x' * 301}, {'message': '/etc/PRIVATE'},
                       {'message': '/data/../etc/PRIVATE'}, {'message': 'token=CANARY'},
                       {'message': 'Bearer CANARY'}, {'message': "token='CANARY words'"},
                       {'message': 'https://user:CANARY@host'}, {'message': 1},
                       {'cause_type': 'CANARY'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                parse_event(json.dumps({**parsed, **fields}))

    def test_truncation_at_data_path_prefix_still_emits_valid_event(self):
        from booksys_core.diagnostics import exception_details
        value = event(service='library', stage='local', code='failed',
            **exception_details(Exception('x' * 296 + ' /data/books/input.cbz')))
        self.assertLessEqual(len(parse_event(json.dumps(value))['message']), 300)

    def test_implicit_cause_and_suppressed_context_follow_python_chaining(self):
        from booksys_core.diagnostics import exception_details
        for suppressed in (False, True):
            try:
                try:
                    raise OSError('private cause')
                except OSError:
                    if suppressed:
                        raise Exception('outer') from None
                    raise Exception('outer')
            except Exception as exc:
                parsed = parse_event(json.dumps(event(service='library', stage='local',
                    code='failed', **exception_details(exc))))
            self.assertEqual(parsed['message'], 'outer')
            self.assertEqual(parsed['cause_type'], None if suppressed else 'OSError')

    def test_uncaught_and_logging_errors_keep_only_safe_metadata(self):
        import logging
        import sys
        from unittest.mock import patch
        from booksys_core import diagnostics as d
        try:
            raise ValueError('password=CANARY https://private.example/source')
        except ValueError as exc:
            record = logging.LogRecord('untrusted', logging.ERROR, 'CANARY', 1, 'CANARY', (), sys.exc_info())
            with patch.object(d, 'emit') as sink:
                d.SafeLoggingHandler('library').emit(record)
            fields = sink.call_args.kwargs
            self.assertEqual(fields['exception_type'], 'ValueError')
            self.assertEqual(fields['reason'], 'invalid_content')
            self.assertNotIn('CANARY', str(fields))
            self.assertNotIn('private.example', str(fields))

    def test_schema_keeps_controlled_queue_outcome_and_task(self):
        value = event(service='library', stage='local', code='completed', reason='none',
                      queue='intake', outcome='done', refs=[{'kind':'task', 'id':'a'*64}])
        self.assertEqual(parse_event(json.dumps(value))['queue'], 'intake')
        with self.assertRaises(ValueError):
            event(service='library', stage='local', code='completed', reason='none', outcome='CANARY')

class SinkRecoveryTests(unittest.TestCase):
    def test_unavailable_sink_rejection_is_bounded_and_observable(self):
        from unittest.mock import patch
        from booksys_core import diagnostics as d
        with patch.object(d, '_default', None), patch.object(d, '_failed_emissions', 0), patch.object(d, 'Emitter', side_effect=OSError('CANARY')):
            started=time.monotonic()
            for _ in range(100):
                self.assertFalse(d.emit(service='library',stage='local',code='completed'))
            self.assertLess(time.monotonic()-started,1)
            self.assertEqual(d.degradation()['rejected_count'],100)

    def test_recovered_sink_emits_lost_and_rejected_counts(self):
        import tempfile
        with tempfile.TemporaryFile() as stream:
            emitter=Emitter(stream.fileno())
            emitter.rejected=2
            emitter.dropped=3
            emitter.emit(event(service='library',stage='local',code='completed',reason='none'))
            emitter.close()
            stream.seek(0)
            rows=[parse_event(line) for line in stream.read().decode().splitlines()]
            self.assertEqual(rows[0]['code'],'recovered')
            self.assertEqual(rows[0]['dropped_count'],3)
            self.assertEqual(rows[0]['rejected_count'],2)
