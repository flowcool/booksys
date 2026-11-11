"""Release CLI/control-plane boundaries use synthetic responses, never GHCR."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import hashlib
import io
import shutil
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
from release_images import publish, release_event, reconcile, registry_manifest
from release_sources import oci_graph, prepare, assemble
from image_validation import identity


def artifact(path, include_attestations=True):
    """Small synthetic OCI index with runtime, SPDX and maximum SLSA v1."""
    blobs = {}

    def blob(document, media):
        content = json.dumps(document).encode()
        sha = hashlib.sha256(content).hexdigest()
        blobs["blobs/sha256/" + sha] = content
        return {"digest": "sha256:" + sha, "size": len(content), "mediaType": media}

    config = blob({"architecture": "amd64", "os": "linux", "config": {"Labels": {
        "org.opencontainers.image.source": "https://github.com/flowcool/booksys",
        "org.opencontainers.image.revision": "a" * 40,
        "org.opencontainers.image.version": "0.1.0"}}}, "application/vnd.oci.image.config.v1+json")
    runtime = blob({"config": config, "layers": []}, "application/vnd.oci.image.manifest.v1+json")
    runtime["platform"] = {"architecture": "amd64", "os": "linux"}
    sbom = blob({"predicateType": "https://spdx.dev/Document", "subject": []}, "application/vnd.in-toto+json")
    provenance = blob({"predicateType": "https://slsa.dev/provenance/v1", "subject": [], "predicate": {"buildDefinition": {
        "internalParameters": {"buildConfig": {"llbDefinition": ["synthetic"]}},
        "externalParameters": {"request": {"args": {"label:" + key: value for key, value in {
            "org.opencontainers.image.source": "https://github.com/flowcool/booksys",
            "org.opencontainers.image.revision": "a" * 40,
            "org.opencontainers.image.version": "0.1.0"}.items()}}}}}}, "application/vnd.in-toto+json")
    attest = blob({"config": config, "layers": [sbom, provenance]}, "application/vnd.oci.image.manifest.v1+json")
    attest["platform"] = {"architecture": "unknown", "os": "unknown"}
    attest["annotations"] = {"vnd.docker.reference.digest": runtime["digest"]}
    root = blob({"manifests": [runtime, attest] if include_attestations else [runtime]}, "application/vnd.oci.image.index.v1+json")
    blobs["index.json"] = json.dumps({"manifests": [root]}).encode()
    with tarfile.open(path, "w") as archive:
        for name, content in blobs.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return blobs["blobs/sha256/" + root["digest"][7:]], config["digest"]

ROOT = Path(__file__).resolve().parents[1]


class ReleaseEventTests(unittest.TestCase):
    def test_first_numeric_prerelease_and_wrong_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            event = {"action": "published", "repository": {"full_name": "flowcool/booksys"},
                     "release": {"tag_name": "v0.1.0", "draft": False, "prerelease": True}}
            path.write_text(json.dumps(event))
            command = [sys.executable, str(ROOT / "ops/release_images.py"), "event", str(path),
                       "--event-name", "release", "--ref", "refs/tags/v0.1.0",
                       "--revision", "a" * 40, "--main", "a" * 40]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["latest"], False)
            self.assertNotEqual(subprocess.run(command[:-1] + ["b" * 40], capture_output=True).returncode, 0)

    def test_validation_events_and_non_numeric_tags_cannot_publish(self):
        for name in ("push", "pull_request", "schedule", "workflow_dispatch"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release_event({}, name, "refs/heads/main", "a" * 40, "a" * 40)
        for tag in ("v01.0.0", "v0.1.0-rc1", "0.1.0", "v0.1", "v0.1.0/evil"):
            event = {"action": "published", "repository": {"full_name": "flowcool/booksys"},
                     "release": {"tag_name": tag, "draft": False, "prerelease": True}}
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release_event(event, "release", "refs/tags/" + tag, "a" * 40, "a" * 40)

    def test_entire_existing_set_is_read_before_any_write(self):
        plan = {"images": {image: {"reference": image, "index_digest": "sha256:bad"}
                           for image in ("worker", "library", "byparr")}}
        with tempfile.TemporaryDirectory() as directory, patch("release_images.registry_manifest", side_effect=[None, b"existing", None]) as read, patch("release_images.run") as write:
            with self.assertRaises(ValueError):
                publish(plan, Path(directory) / "journal.json")
            self.assertEqual(read.call_count, 3)
            write.assert_not_called()

    def test_explicit_absence_differs_from_uncertain_reads(self):
        for error in ('reading manifest x in ghcr.io/flowcool/booksys-worker: manifest unknown"\n',
                      'reading manifest x in ghcr.io/flowcool/booksys-worker: name unknown"\n'):
            with self.subTest(error=error), patch("release_images.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", error.encode())):
                self.assertIsNone(registry_manifest("ghcr.io/flowcool/booksys-worker:x"))
        for error in ("unauthorized: authentication required", "timeout", "HTTP 503", "manifest unknown in proxy HTML"):
            with self.subTest(error=error), patch("release_images.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", error.encode())), self.assertRaises(RuntimeError):
                registry_manifest("ghcr.io/flowcool/booksys-worker:x")


class ReleasePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.remote = {}
        self.calls = []
        self.assets = []
        self.failure = None
        self.plan = {"tag": "v0.1.0", "version": "0.1.0", "revision": "a" * 40,
                     "identity": "https://github.com/flowcool/booksys/.github/workflows/release.yml@refs/tags/v0.1.0",
                     "latest": False, "images": {}, "source_assets": [], "asset_digests": {}}
        for image in ("worker", "library", "byparr"):
            path = self.directory / (image + ".oci.tar")
            self.raw, _ = artifact(path)
            graph = oci_graph(path)
            self.plan["images"][image] = {**graph, "archive": str(path),
                "reference": "ghcr.io/flowcool/booksys-" + image + ":0.1.0"}
        self.addCleanup(patch.stopall)
        patch("release_images.registry_manifest", side_effect=lambda reference: self.remote.get(reference)).start()
        patch("release_images.run", side_effect=self.control_plane).start()

    def control_plane(self, *command):
        self.calls.append(command)
        if command[:2] == ("gh", "api"):
            return json.dumps({"tag_name": "v0.1.0", "prerelease": False, "draft": False, "assets": self.assets})
        if command[:3] == ("gh", "release", "upload"):
            path = command[4]
            self.assets.append({"name": Path(path).name, "digest": self.plan["asset_digests"][path]})
        if command[:3] == ("skopeo", "inspect", "--config"):
            return json.dumps({"config": {"Labels": {"org.opencontainers.image.version": "0.0.9"}}})
        if command[:2] == ("skopeo", "copy"):
            source, target = command[-2:]
            if target.startswith("oci-archive:"):
                package = source[9:].split("@")[0]
                image = package.rsplit("-", 1)[1]
                shutil.copyfile(self.plan["images"][image]["archive"], target[12:])
            else:
                self.remote[target[9:]] = self.raw
            if self.failure and self.failure in target:
                raise subprocess.CalledProcessError(1, command, "", "synthetic connection lost after write")
        if command[:2] == ("cosign", "verify"):
            return '[{"critical":{"identity":{"docker-reference":"synthetic"}}}]'
        return ""

    def test_initial_prerelease_publishes_all_exact_graphs_without_latest_or_rebuild(self):
        publish(self.plan, self.directory / "journal.json")
        self.assertEqual(reconcile(self.plan), {"worker": "match", "library": "match", "byparr": "match"})
        self.assertFalse(any(":latest" in argument or "buildx" in argument for command in self.calls for argument in command))
        self.assertEqual(sum(command[:2] == ("cosign", "verify") for command in self.calls), 3)
        self.assertTrue(json.loads((self.directory / "journal.json").read_text())["complete"])

    def test_uncertain_write_stops_without_resend_or_stable_promotion(self):
        self.failure = "booksys-library:"
        with self.assertRaises(subprocess.CalledProcessError):
            publish(self.plan, self.directory / "journal.json")
        self.assertEqual(reconcile(self.plan), {"worker": "match", "library": "match", "byparr": "absent"})
        journal = json.loads((self.directory / "journal.json").read_text())
        self.assertEqual(journal["pending"], "copy:library")
        self.assertEqual(journal["failure"]["stderr"], "synthetic connection lost after write")
        self.assertEqual(sum(command[-1].endswith("booksys-library:0.1.0") for command in self.calls), 1)

    def test_partial_stable_promotion_keeps_verified_versions_and_stops(self):
        self.plan["latest"] = True
        self.failure = "booksys-library:latest"
        with self.assertRaises(subprocess.CalledProcessError):
            publish(self.plan, self.directory / "journal.json")
        self.assertEqual(reconcile(self.plan), {"worker": "match", "library": "match", "byparr": "match"})
        self.assertIn("ghcr.io/flowcool/booksys-worker:latest", self.remote)
        self.assertNotIn("ghcr.io/flowcool/booksys-byparr:latest", self.remote)
        self.assertEqual(json.loads((self.directory / "journal.json").read_text())["pending"], "latest:library")

    def test_duplicate_version_stops_before_assets_signatures_or_aliases(self):
        self.remote[self.plan["images"]["byparr"]["reference"]] = self.raw
        with self.assertRaises(ValueError):
            publish(self.plan, self.directory / "journal.json")
        self.assertEqual(self.calls, [])

    def test_source_assets_are_verified_before_images(self):
        path = self.directory / "corresponding-source.tar.gz"
        path.write_bytes(b"retained synthetic source")
        self.plan["source_assets"] = [str(path)]
        self.plan["asset_digests"] = {str(path): "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
        publish(self.plan, self.directory / "journal.json")
        upload = next(i for i, command in enumerate(self.calls) if command[:3] == ("gh", "release", "upload"))
        copy = next(i for i, command in enumerate(self.calls) if command[:2] == ("skopeo", "copy"))
        self.assertLess(upload, copy)

    def test_approved_recovery_fills_only_absent_versions(self):
        self.remote[self.plan["images"]["worker"]["reference"]] = self.raw
        publish(self.plan, self.directory / "recovery.json", resume=True)
        copies = [command for command in self.calls if command[:2] == ("skopeo", "copy") and command[-1].startswith("docker://")]
        self.assertEqual(len(copies), 2)
        self.assertFalse(any(command[-1].endswith("booksys-worker:0.1.0") for command in copies))

    def test_conflicting_version_blocks_even_approved_recovery(self):
        self.remote[self.plan["images"]["library"]["reference"]] = b"different digest"
        with self.assertRaises(ValueError):
            publish(self.plan, self.directory / "recovery.json", resume=True)
        self.assertEqual(self.calls, [])


class ArtifactQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.release = {"tag": "v0.1.0", "version": "0.1.0", "revision": "a" * 40, "latest": False,
                        "identity": "https://github.com/flowcool/booksys/.github/workflows/release.yml@refs/tags/v0.1.0"}
        self.sources = self.directory / "sources"
        self.sources.mkdir()
        (self.directory / "corresponding-source.tar.gz").write_bytes(b"synthetic source bundle")
        manifest = {"revision": "a" * 40, "images": {}}
        for image in ("worker", "library", "byparr"):
            directory = self.directory / image
            directory.mkdir()
            path = directory / "image.oci.tar"
            _, config = artifact(path)
            (directory / "identity.json").write_text(json.dumps(identity(path, "a" * 40)))
            for report in ("vulnerabilities", "secrets"):
                (directory / (report + ".json")).write_text(json.dumps({"Metadata": {"ImageID": config}, "Results": []}))
            manifest["images"][image] = {"index_digest": oci_graph(path)["index_digest"]}
        (self.sources / "manifest.json").write_text(json.dumps(manifest))

    def test_exact_scanned_attested_set_qualifies_and_fixable_severe_blocks(self):
        self.assertEqual(set(prepare(self.release, self.directory, self.sources)["images"]), {"worker", "library", "byparr"})
        path = self.directory / "library" / "vulnerabilities.json"
        report = json.loads(path.read_text())
        report["Results"] = [{"Vulnerabilities": [{"Severity": "HIGH", "FixedVersion": "2", "VulnerabilityID": "synthetic-CVE"}]}]
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "security gate"):
            prepare(self.release, self.directory, self.sources)

    def test_mismatched_scan_fails_closed(self):
        path = self.directory / "worker" / "secrets.json"
        path.write_text(json.dumps({"Metadata": {"ImageID": "sha256:wrong"}, "Results": []}))
        with self.assertRaisesRegex(ValueError, "Scanner"):
            prepare(self.release, self.directory, self.sources)

    def test_missing_sbom_and_provenance_refuses_an_otherwise_valid_image(self):
        path = self.directory / "worker" / "image.oci.tar"
        artifact(path, include_attestations=False)
        (path.parent / "identity.json").write_text(json.dumps(identity(path, "a" * 40)))
        with self.assertRaisesRegex(ValueError, "SBOM"):
            prepare(self.release, self.directory, self.sources)

    def test_exact_source_version_labels_are_mandatory(self):
        for key, value in (("revision", "b" * 40), ("version", "0.2.0")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                prepare({**self.release, key: value}, self.directory, self.sources)

    def test_unknown_source_inventory_refuses_before_download(self):
        lock = {"images": {"worker": {"inventory": [], "reviewed_non_package_components": []}}}
        (self.directory / "worker" / "inventory.json").write_text('[{"id":"dpkg:unknown","version":"1"}]')
        with patch("release_sources.urlopen") as download, self.assertRaisesRegex(ValueError, "inventory"):
            assemble(lock, self.directory, self.directory / "assembled", self.release)
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
