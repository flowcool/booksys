"""Public proof rejects altered source bytes before claiming a download."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from verify_public_release import download
from recover_first_release import split_source


class PublicSourceProofTests(unittest.TestCase):
    def test_source_parts_reassemble_exactly_and_reject_a_changed_whole(self):
        content = b"synthetic source bytes"
        expected = "sha256:" + hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source.tar.gz"
            archive.write_bytes(content)
            output = root / "parts"
            output.mkdir()
            result = split_source(archive, output, expected, part_size=12)
            self.assertEqual(len(result["parts"]), 2)
            whole = hashlib.sha256()
            for part in result["parts"]:
                data = (output / part["name"]).read_bytes()
                response = io.BytesIO(data)
                response.url = "https://github.com/public-part"
                with patch("verify_public_release.urlopen", return_value=response):
                    download(response.url, part["sha256"], whole=whole)
            self.assertEqual("sha256:" + whole.hexdigest(), expected)
            bad = root / "bad"
            bad.mkdir()
            with self.assertRaisesRegex(ValueError, "differs"):
                split_source(archive, bad, "sha256:" + "0" * 64, part_size=12)

    def test_signed_graph_binding_accepts_empty_subjects_and_rejects_conflicts(self):
        from test_release_images import artifact
        from release_sources import oci_graph
        from verify_public_release import verify_image
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "image.tar"
            artifact(archive)
            graph = oci_graph(archive)
            self.assertEqual(verify_image(archive, "a" * 40, graph["index_digest"])["platform"], "linux/amd64")
            graph["statements"][0]["runtime_digest"] = "sha256:" + "0" * 64
            with patch("verify_public_release.oci_graph", return_value=graph), self.assertRaisesRegex(ValueError, "bind"):
                verify_image(archive, "a" * 40, graph["index_digest"])
            graph = oci_graph(archive)
            graph["statements"][0]["statement"]["subject"] = [{"digest": {"sha256": "0" * 64}}]
            with patch("verify_public_release.oci_graph", return_value=graph), self.assertRaisesRegex(ValueError, "bind"):
                verify_image(archive, "a" * 40, graph["index_digest"])

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
