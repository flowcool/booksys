"""Artifact and security gates at the retained OCI boundary."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from ops.image_validation import identity, security_findings


class ImageValidationTests(unittest.TestCase):
    def archive(self, directory, *, revision="a" * 40, architecture="amd64"):
        blobs = {}

        def blob(value):
            content = json.dumps(value).encode()
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            blobs["blobs/sha256/" + digest.split(":")[1]] = content
            return {"digest": digest, "size": len(content)}

        config = blob({"architecture": architecture, "os": "linux", "config": {
            "Labels": {"org.opencontainers.image.revision": revision,
                       "org.opencontainers.image.source": "https://github.com/flowcool/booksys"}}})
        manifest = blob({"config": config, "layers": []})
        manifest["platform"] = {"os": "linux", "architecture": architecture}
        attestation = blob({"not": "an executable manifest"})
        attestation["platform"] = {"os": "unknown", "architecture": "unknown"}
        index = blob({"manifests": [manifest, attestation]})
        blobs["index.json"] = json.dumps({"manifests": [index]}).encode()
        path = Path(directory) / "image.tar"
        with tarfile.open(path, "w") as archive:
            for name, content in blobs.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        return path, config["digest"], manifest["digest"]

    def test_retained_attested_artifact_selects_exact_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config, manifest = self.archive(directory)
            result = identity(path, "a" * 40)
            self.assertEqual(result["config_digest"], config)
            self.assertEqual(result["manifest_digest"], manifest)
            self.assertTrue(result["archive_sha256"].startswith("sha256:"))

    def test_wrong_source_revision_or_platform_refuses_artifact(self):
        for arguments in ({"revision": "b" * 40}, {"architecture": "arm64"}):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as directory:
                path, _, _ = self.archive(directory, **arguments)
                with self.assertRaises(ValueError):
                    identity(path, "a" * 40)

    def test_fixable_severe_blocks_but_unfixed_remains_reported(self):
        report = {"Results": [{"Vulnerabilities": [
            {"VulnerabilityID": "FIXED", "Severity": "HIGH", "FixedVersion": "2"},
            {"VulnerabilityID": "UNFIXED", "Severity": "CRITICAL"},
            {"VulnerabilityID": "LOW", "Severity": "LOW", "FixedVersion": "2"}]}]}
        self.assertEqual(security_findings(report, {"Results": []}), ["vulnerability:FIXED"])

    def test_secrets_block_at_any_severity_and_empty_scan_cannot_pass(self):
        self.assertEqual(security_findings({"Results": []}, {"Results": [
            {"Secrets": [{"RuleID": "fixture", "Severity": "LOW"}]}]}), ["secret:fixture"])
        with self.assertRaises(ValueError):
            security_findings({}, {})

    def test_gate_refuses_a_report_for_a_different_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "identity.json").write_text(json.dumps({"config_digest": "sha256:expected"}))
            for name in ("vulnerabilities", "secrets"):
                (root / (name + ".json")).write_text(json.dumps({
                    "Metadata": {"ImageID": "sha256:different"}, "Results": []}))
            result = subprocess.run([sys.executable, "ops/image_validation.py", "gate",
                                     str(root / "identity.json"), str(root / "vulnerabilities.json"),
                                     str(root / "secrets.json")], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Scanned image differs", result.stderr)
