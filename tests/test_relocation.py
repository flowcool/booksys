"""Retained physical proofs need an explicit verified relocation mapping."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from library.readers import fingerprint
from library.relocation import record_relocation, matches_retained


class RelocationTests(unittest.TestCase):
    def test_copy_preserves_proof_only_through_exact_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target, receipt = root/'old.cbz', root/'new.cbz', root/'relocation.json'
            source.write_bytes(b'original source')
            original = list(fingerprint(source))
            shutil.copy2(source, target)
            self.assertFalse(matches_retained(target, original))
            checksum = hashlib.sha256(b'original source').hexdigest()
            record_relocation([(source, target, target)], receipt)
            with patch.dict(os.environ, {'BOOKSYS_RELOCATION_RECEIPT': str(receipt)}):
                self.assertTrue(matches_retained(target, original, checksum))
                self.assertFalse(matches_retained(target, original, '0'*64))
                target.write_bytes(b'changed source!')
                self.assertFalse(matches_retained(target, original, checksum))
            self.assertEqual(source.read_bytes(), b'original source')

    def test_same_bytes_do_not_authorize_unmapped_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target, other, receipt = (root/n for n in ('a', 'b', 'c', 'receipt.json'))
            source.write_bytes(b'book')
            shutil.copy2(source, target)
            shutil.copy2(source, other)
            record_relocation([(source, target, target)], receipt)
            with patch.dict(os.environ, {'BOOKSYS_RELOCATION_RECEIPT': str(receipt)}):
                self.assertFalse(matches_retained(other, list(fingerprint(source))))
