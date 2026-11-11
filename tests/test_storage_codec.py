"""Accepted lossless legacy export seam. No live data or services."""
from datetime import date, datetime, timezone
import math
import unittest

from library.codec import decode, encode, CodecError


class CodecTests(unittest.TestCase):
    def test_roundtrip_extensions_native_values_and_reserved_tags(self):
        value = {'unknown': {'$booksys': ['date', 'not a date']}, 'null': None,
                 'binary': b'\x00\xff', 'date': date(2026, 11, 9),
                 'time': datetime(2026, 11, 9, tzinfo=timezone.utc),
                 'set': {'x', 'y'}, 'integer': 2 ** 80, 'float': float('inf')}
        self.assertEqual(decode(encode(value)), value)
        self.assertTrue(math.isnan(decode(encode(float('nan')))))
        self.assertNotIn('missing', decode(encode(value)))

    def test_recursive_aliases_and_executable_values_are_refused(self):
        recursive = []
        recursive.append(recursive)
        for value in (recursive, object(), {1: 'non-string key'}):
            with self.subTest(value=type(value).__name__), self.assertRaises(CodecError):
                encode(value)

    def test_shared_alias_is_value_preserving_and_bad_tags_fail_closed(self):
        shared = ['same', None]
        self.assertEqual(decode(encode({'one': shared, 'two': shared})),
                         {'one': ['same', None], 'two': ['same', None]})
        for payload in ('{"$booksys":["execute","bad"]}', 'NaN', '{"x":1,"x":2}'):
            with self.subTest(payload=payload), self.assertRaises(CodecError):
                decode(payload)
