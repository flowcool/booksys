"""Public proof rejects altered source bytes before claiming a download."""
import hashlib
import io
import json
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from verify_public_release import download, main as verify_main
from recover_first_release import split_source


class PublicSourceProofTests(unittest.TestCase):
    def test_source_verifier_accepts_three_parts_and_single_archive(self):
        content = b"synthetic source bytes"
        expected = "sha256:" + hashlib.sha256(content).hexdigest()
        base = "https://github.com/flowcool/booksys/releases/download/v0.1.0/"
        for count in (3, 2, 0):
            chunks = [content[index:index + 8] for index in range(0, len(content), 8)] if count == 3 else [content[:12], content[12:]]
            payloads = {f"corresponding-source.tar.gz.part-{index:03d}": chunk for index, chunk in enumerate(chunks)} if count else {"corresponding-source.tar.gz": content}
            manifest = {"revision": "a" * 40, "images": dict.fromkeys(("worker", "library", "byparr"), {})}
            if count:
                manifest["source_archive"] = {"name": "corresponding-source.tar.gz", "size": len(content), "sha256": expected,
                    "parts": [{"name": name, "size": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "url": base + name} for name, data in payloads.items()],
                    "reassemble": "cat " + " ".join(payloads) + " > corresponding-source.tar.gz"}
            variants = ["valid", "asset_digest", "download_bytes"]
            if count:
                variants += ["part_size", "whole_size", "whole_digest", "part_order", "part_limit", "part_url", "one_part"]
            for variant in variants:
                with self.subTest(parts=count, variant=variant), tempfile.TemporaryDirectory() as temporary:
                    document = copy.deepcopy(manifest)
                    if variant == "part_size":
                        document["source_archive"]["parts"][0]["size"] += 1
                    elif variant == "whole_size":
                        document["source_archive"]["size"] += 1
                    elif variant == "whole_digest":
                        document["source_archive"]["sha256"] = "sha256:" + "0" * 64
                    elif variant == "part_order":
                        document["source_archive"]["parts"].reverse()
                    elif variant == "part_limit":
                        document["source_archive"]["parts"][0]["size"] = 2 * 1024**3
                    elif variant == "part_url":
                        document["source_archive"]["parts"][0]["url"] = base + "wrong"
                    elif variant == "one_part":
                        document["source_archive"]["parts"] = document["source_archive"]["parts"][:1]
                    files = {**payloads, "manifest.json": json.dumps(document).encode()}
                    assets = [{"name": name, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "browser_download_url": base + name} for name, data in files.items()]
                    if variant == "asset_digest":
                        assets[0]["digest"] = "sha256:" + "0" * 64
                    if variant == "download_bytes":
                        files[next(iter(payloads))] = b"changed"
                    release = {"tag_name": "v0.1.0", "draft": False, "prerelease": True, "target_commitish": "a" * 40,
                               "html_url": "https://github.com/flowcool/booksys/releases/tag/v0.1.0", "assets": assets}

                    def response(request, **kwargs):
                        url = request.full_url
                        data = json.dumps(release).encode() if url.startswith("https://api.github.com/") else files[url.removeprefix(base)]
                        result = io.BytesIO(data)
                        result.url = url
                        return result

                    output = Path(temporary) / "proof"
                    argv = ["verify_public_release.py", "--revision", "a" * 40, "--manifest-sha256", assets[-1]["digest"],
                            "--source-sha256", expected, "--output", str(output)]
                    with patch.object(sys, "argv", argv), patch("verify_public_release.urlopen", side_effect=response):
                        if variant == "valid":
                            verify_main()
                            proof = json.loads((output / "anonymous-source-proof.json").read_text())
                            self.assertEqual(proof["whole_archive"], {"sha256": expected, "size": len(content)})
                            self.assertEqual(set(proof["assets"]), set(files))
                        else:
                            with self.assertRaises(ValueError):
                                verify_main()

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
