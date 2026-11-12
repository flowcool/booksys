"""Artifact and security gates at the retained OCI boundary."""

import hashlib
import io
import json
import os
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
            with tarfile.open(path) as archive:
                root = json.load(archive.extractfile("index.json"))
            self.assertEqual(result["index_digest"], root["manifests"][0]["digest"])
            self.assertNotEqual(result["index_digest"], manifest)
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
            {"Secrets": [{"RuleID": "fixture", "Severity": "LOW", "Match": "synthetic-sensitive-value"}]}]}), ["secret-count:1"])
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

    def readiness_failure(self, directory, *, probe=42, logs=0, inspect=0, stop=0):
        root = Path(directory)
        archive, _, manifest = self.archive(root)
        docker = root / "docker"
        docker.write_text("#!" + sys.executable + "\n" + r'''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["CALLS"]).open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if args[:2] == ["image", "inspect"]:
    print(os.environ["MANIFEST"] if "--format" in args else "[{}]")
elif args[0] == "run":
    print("owned-readiness-probe")
elif args[0] == "exec":
    raise SystemExit(int(os.environ["PROBE_RESULT"]))
elif args[0] == "logs":
    print("retained startup log")
    raise SystemExit(int(os.environ["LOGS_RESULT"]))
elif args[0] == "inspect":
    print("[{}]")
    raise SystemExit(int(os.environ["INSPECT_RESULT"]))
elif args[0] == "stop":
    raise SystemExit(int(os.environ["STOP_RESULT"]))
''')
        docker.chmod(0o755)
        env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                   MANIFEST=identity(archive, "a" * 40)["index_digest"], CALLS=str(root / "calls.jsonl"),
                   PROBE_RESULT=str(probe), LOGS_RESULT=str(logs),
                   INSPECT_RESULT=str(inspect), STOP_RESULT=str(stop))
        result = subprocess.run(["bash", "ops/validate_image.sh", "byparr", str(archive),
                                 "a" * 40, str(root / "reports")], env=env,
                                capture_output=True, text=True)
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        return result, calls

    def test_failed_evidence_capture_or_stop_preserves_probe_failure_and_attempts_stop(self):
        for failure in ("logs", "inspect", "stop"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                result, calls = self.readiness_failure(directory, **{failure: 7})
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertIn(["stop", "--time", "10", "owned-readiness-probe"], calls)
                self.assertNotIn(["rm", "owned-readiness-probe"], calls)

    def test_failed_cleanup_fails_a_successful_readiness_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.readiness_failure(directory, probe=0, logs=7)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(["stop", "--time", "10", "owned-readiness-probe"], calls)

    def test_startup_failure_keeps_logs_until_stop_and_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.readiness_failure(directory)
            self.assertEqual(result.returncode, 42, result.stderr)
            self.assertNotIn("--rm", next(c for c in calls if c[0] == "run" and "-d" in c))
            self.assertLess(calls.index(["logs", "owned-readiness-probe"]),
                            calls.index(["stop", "--time", "10", "owned-readiness-probe"]))
            self.assertEqual(calls[-1], ["rm", "owned-readiness-probe"])
            self.assertIn("retained startup log",
                          (Path(directory) / "reports" / "entrypoint.log").read_text())
