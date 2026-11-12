"""Public proof rejects altered source bytes before claiming a download."""
import hashlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from verify_public_release import download


class PublicSourceProofTests(unittest.TestCase):
    def test_anonymous_download_checks_exact_bytes_and_https(self):
        content = b"synthetic corresponding source"
        expected = "sha256:" + hashlib.sha256(content).hexdigest()

        def response(url="https://release-assets.githubusercontent.com/source"):
            result = io.BytesIO(content)
            result.url = url
            return result

        with patch("verify_public_release.urlopen", return_value=response()) as opening:
            self.assertEqual(download("https://github.com/source", expected, True), (content, len(content)))
            request = opening.call_args.args[0]
            self.assertFalse(request.has_header("Authorization"))
        with patch("verify_public_release.urlopen", return_value=response()), self.assertRaisesRegex(ValueError, "differs"):
            download("https://github.com/source", "sha256:" + "0" * 64)
        with patch("verify_public_release.urlopen", return_value=response("http://invalid/source")), self.assertRaisesRegex(ValueError, "HTTPS"):
            download("https://github.com/source", expected)


if __name__ == "__main__":
    unittest.main()
