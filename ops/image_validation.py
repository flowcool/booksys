"""Identify the retained OCI artifact and enforce the image security gate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tarfile
from typing import Any

SOURCE = "https://github.com/flowcool/booksys"


def identity(path: Path, revision: str) -> dict[str, str]:
    with tarfile.open(path) as archive:
        def read(name: str) -> dict[str, Any]:
            stream = archive.extractfile(name)
            if stream is None:
                raise ValueError(f"Missing OCI member: {name}")
            content = stream.read()
            if name.startswith("blobs/sha256/") and hashlib.sha256(content).hexdigest() != name.rsplit("/", 1)[1]:
                raise ValueError("OCI blob digest mismatch")
            result: dict[str, Any] = json.loads(content)
            return result

        def blob(descriptor: dict[str, Any]) -> dict[str, Any]:
            algorithm, digest = descriptor["digest"].split(":", 1)
            if algorithm != "sha256" or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Unsupported OCI digest")
            return read("blobs/sha256/" + digest)

        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []

        def visit(descriptor: dict[str, Any], depth: int = 0) -> None:
            if depth > 4:
                raise ValueError("Unexpected OCI index nesting")
            document = blob(descriptor)
            if "manifests" in document:
                for child in document["manifests"]:
                    visit(child, depth + 1)
            elif descriptor.get("platform", {}).get("architecture") != "unknown":
                config = blob(document["config"])
                if config.get("architecture") == "amd64" and config.get("os") == "linux":
                    candidates.append((descriptor, document))

        index = read("index.json")
        if len(index["manifests"]) != 1:
            raise ValueError("Expected exactly one retained OCI root descriptor")
        root_descriptor = index["manifests"][0]
        visit(root_descriptor)
        if len(candidates) != 1:
            raise ValueError("Expected exactly one linux/amd64 runtime image")
        descriptor, manifest = candidates[0]
        config = blob(manifest["config"])
        labels = config.get("config", {}).get("Labels", {})
        if labels.get("org.opencontainers.image.revision") != revision or labels.get("org.opencontainers.image.source") != SOURCE:
            raise ValueError("Image source labels do not match the requested source")
        image_id = str(manifest["config"]["digest"])
    with path.open("rb") as stream:
        archive_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"source": SOURCE, "revision": revision, "platform": "linux/amd64",
            "config_digest": image_id, "manifest_digest": str(descriptor["digest"]),
            "index_digest": str(root_descriptor["digest"]),
            "archive_sha256": "sha256:" + archive_digest}


def security_findings(vulnerabilities: dict[str, Any], scan_report: dict[str, Any]) -> list[str]:
    if not isinstance(vulnerabilities.get("Results"), list) or not isinstance(scan_report.get("Results"), list):
        raise ValueError("Missing Trivy scan results")
    findings = []
    for result in vulnerabilities["Results"]:
        for finding in result.get("Vulnerabilities", []):
            if finding["Severity"] in {"HIGH", "CRITICAL"} and finding.get("FixedVersion"):
                findings.append("vulnerability:" + finding["VulnerabilityID"])
    # Report rule identifiers only. Matched bytes remain in the retained scanner report.
    for result in scan_report["Results"]:
        for finding in result.get("Secrets", []):
            findings.append("secret:" + finding["RuleID"])
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    identify = commands.add_parser("identity")
    identify.add_argument("archive", type=Path)
    identify.add_argument("revision")
    gate = commands.add_parser("gate")
    gate.add_argument("identity", type=Path)
    gate.add_argument("vulnerabilities", type=Path)
    gate.add_argument("secrets", type=Path)
    args = parser.parse_args()
    if args.command == "identity":
        print(json.dumps(identity(args.archive, args.revision), indent=2))
        return 0
    expected = json.loads(args.identity.read_text())
    vulnerabilities = json.loads(args.vulnerabilities.read_text())
    scan_report = json.loads(args.secrets.read_text())
    for report in (vulnerabilities, scan_report):
        if report.get("Metadata", {}).get("ImageID") != expected["config_digest"]:
            raise ValueError("Scanned image differs from retained OCI artifact")
    findings = security_findings(vulnerabilities, scan_report)
    print(json.dumps({"blocking_findings": findings}, indent=2))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
