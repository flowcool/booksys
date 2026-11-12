"""Verify exact OCI graphs and assemble retained, reviewed corresponding source."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from image_validation import identity, security_findings
from release_images import IMAGES, digest


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def oci_graph(path: Path) -> dict[str, Any]:
    """Hash all referenced bytes, including layers and in-index attestations."""
    blobs: dict[str, int] = {}
    statements: list[dict[str, Any]] = []
    with tarfile.open(path) as archive:
        def read(name: str) -> bytes:
            member = archive.getmember(name)
            if not member.isfile():
                raise ValueError("OCI member is not a regular file")
            stream = archive.extractfile(member)
            assert stream is not None
            return stream.read()

        def walk(descriptor: dict[str, Any], depth: int = 0, runtime: str = "") -> None:
            if depth > 8 or not re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor["digest"]):
                raise ValueError("Invalid OCI graph")
            expected = descriptor["digest"]
            if expected in blobs:
                return
            member = archive.getmember("blobs/sha256/" + expected[7:])
            if not member.isfile() or member.size != descriptor["size"]:
                raise ValueError("OCI blob size/type mismatch")
            stream = archive.extractfile(member)
            assert stream is not None
            hasher = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
            if "sha256:" + hasher.hexdigest() != expected:
                raise ValueError("OCI blob hash mismatch")
            blobs[expected] = member.size
            media = descriptor["mediaType"]
            runtime = descriptor.get("annotations", {}).get("vnd.docker.reference.digest", runtime)
            if "index" in media or "manifest" in media:
                document = json.loads(read(member.name))
                for child in document.get("manifests", []) + document.get("layers", []):
                    walk(child, depth + 1, runtime)
                if "config" in document:
                    walk(document["config"], depth + 1)
            elif media == "application/vnd.in-toto+json":
                statements.append({"statement": json.loads(read(member.name)), "runtime_digest": runtime})

        index = json.loads(read("index.json"))
        # BuildKit wraps the release image index in one OCI-layout descriptor.
        if len(index["manifests"]) != 1:
            raise ValueError("Expected one named OCI release root index")
        root = index["manifests"][0]
        walk(root)
    return {"index_digest": root["digest"], "blobs": blobs, "statements": statements}


def fetch(url: str, expected: str, destination: Path) -> dict[str, str]:
    if urlparse(url).scheme != "https" or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Source needs HTTPS and an independently reviewed SHA256")
    if destination.exists():
        if file_digest(destination) != "sha256:" + expected:
            raise ValueError("Preserved download differs; never overwrite it")
    else:
        with urlopen(url, timeout=120) as response, destination.open("xb") as output:
            if urlparse(response.url).scheme != "https":
                raise ValueError("Source download redirected outside HTTPS")
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if file_digest(destination) != "sha256:" + expected:
            raise ValueError("Source checksum mismatch; preserved for investigation")
    return {"file": destination.name, "url": url, "sha256": expected}


def fetch_dsc(url: str, expected: str, destination: Path) -> list[dict[str, str]]:
    destination.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(url).path).name
    if not name.endswith(".dsc"):
        raise ValueError("Expected Debian/Ubuntu source control file")
    files = [fetch(url, expected, destination / name)]
    content = (destination / name).read_text()
    match = re.search(r"^Checksums-Sha256:\n((?: [^\n]+\n)+)", content, re.M)
    if match is None:
        raise ValueError("DSC lacks SHA256 source/patch checksums")
    for line in match[1].splitlines():
        checksum, size, filename = line.split()
        if Path(filename).name != filename:
            raise ValueError("Unsafe source filename")
        files.append(fetch(urljoin(url, filename), checksum, destination / filename))
        if (destination / filename).stat().st_size != int(size):
            raise ValueError("Source archive size mismatch")
    return files


def assemble(lock: dict[str, Any], artifacts: Path, destination: Path, release: dict[str, Any]) -> dict[str, Any]:
    """No inferred licenses. Exact complete inventories must match a reviewed lock."""
    destination.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"revision": release["revision"], "images": {}, "sources": []}
    for image in IMAGES:
        inventory = json.loads((artifacts / image / "inventory.json").read_text())
        approved = lock["images"][image]
        if approved["inventory"] != inventory or not approved.get("reviewed_non_package_components"):
            raise ValueError("Exact installed inventory or non-package source review missing: " + image)
        covered = set()
        for component in approved["components"]:
            if component["id"] in covered:
                raise ValueError("Duplicate source coverage")
            covered.add(component["id"])
            if component.get("exclusion"):
                if not component.get("rationale") or not component.get("license"):
                    raise ValueError("Source exclusion needs individual license/rationale")
            elif not component.get("sources") or any(source not in lock["sources"] for source in component["sources"]):
                raise ValueError("Component lacks retained source: " + component["id"])
        if covered != {entry["id"] for entry in inventory} | set(approved["reviewed_non_package_components"]):
            raise ValueError("Source ledger must cover every installed and non-package component")
        graph = oci_graph(artifacts / image / "image.oci.tar")
        manifest["images"][image] = {"index_digest": graph["index_digest"], **approved}
        notices = artifacts / image / "notices.tar"
        if not notices.is_file():
            raise ValueError("Retained image license/notice files missing")
        (destination / (image + "-notices.tar")).write_bytes(notices.read_bytes())
    source_dir = destination / "source"
    source_dir.mkdir()
    for name, source in lock["sources"].items():
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError("Invalid source identifier")
        target = source_dir / name
        target.mkdir()
        if source["url"].endswith(".dsc"):
            files = fetch_dsc(source["url"], source["sha256"], target)
            control = (target / files[0]["file"]).read_text()
            for field, key in (("Source", "package"), ("Version", "version")):
                match = re.search(r"^" + field + r": (.+)$", control, re.M)
                if match is None or match[1] != source.get(key):
                    raise ValueError("DSC source package/version differs from the reviewed source ledger")
        else:
            files = [fetch(source["url"], source["sha256"], target / Path(urlparse(source["url"]).path).name)]
        manifest["sources"].append({"id": name, **source, "files": files})
    # Include the complete exact public revision: Dockerfiles, adapters, locks and build instructions.
    subprocess.run(["git", "archive", "--format=tar", "-o", str(destination / "booksys-source.tar"), release["revision"]], check=True)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    bundle = destination.parent / "corresponding-source.tar.gz"
    with tarfile.open(bundle, "x:gz") as archive:
        archive.add(destination, arcname="corresponding-source")
    return {"file": str(bundle), "sha256": file_digest(bundle)}


def prepare(release: dict[str, Any], artifacts: Path, sources: Path) -> dict[str, Any]:
    manifest = json.loads((sources / "manifest.json").read_text())
    if manifest["revision"] != release["revision"]:
        raise ValueError("Source bundle revision mismatch")
    plan = {**release, "images": {}, "source_assets": [str(sources.parent / "corresponding-source.tar.gz"), str(sources / "manifest.json")]}
    for image in IMAGES:
        directory = artifacts / image
        archive = directory / "image.oci.tar"
        expected = identity(archive, release["revision"])
        graph = oci_graph(archive)
        with tarfile.open(archive) as tar:
            stream = tar.extractfile("blobs/sha256/" + expected["config_digest"][7:])
            assert stream is not None
            labels = json.load(stream)["config"]["Labels"]
        if labels.get("org.opencontainers.image.version") != release["version"]:
            raise ValueError("Release version label mismatch")
        types = set()
        for attestation in graph.pop("statements"):
            statement = attestation["statement"]
            subjects = statement.get("subject", [])
            if attestation["runtime_digest"] != expected["manifest_digest"] or (subjects and not any(subject.get("digest", {}).get("sha256") == expected["manifest_digest"][7:] for subject in subjects)):
                raise ValueError("Attestation does not bind the tested runtime manifest")
            if statement.get("predicateType") == "https://slsa.dev/provenance/v1" and not statement.get("predicate", {}).get("buildDefinition", {}).get("internalParameters", {}).get("buildConfig"):
                raise ValueError("Maximum provenance build configuration missing")
            if statement.get("predicateType") == "https://slsa.dev/provenance/v1":
                args = statement["predicate"]["buildDefinition"]["externalParameters"]["request"]["args"]
                for label in ("source", "revision", "version"):
                    key = "org.opencontainers.image." + label
                    if args.get("label:" + key) != labels[key]:
                        raise ValueError("Provenance source/version request differs from the runtime config")
            types.add(statement.get("predicateType"))
        if not {"https://spdx.dev/Document", "https://slsa.dev/provenance/v1"} <= types:
            raise ValueError("SPDX SBOM and SLSA provenance are required")
        for name in ("vulnerabilities", "secrets"):
            report = json.loads((directory / (name + ".json")).read_text())
            if report.get("Metadata", {}).get("ImageID") != expected["config_digest"]:
                raise ValueError("Scanner did not inspect the tested artifact")
        if security_findings(json.loads((directory / "vulnerabilities.json").read_text()), json.loads((directory / "secrets.json").read_text())):
            raise ValueError("Release security gate failed")
        if json.loads((directory / "identity.json").read_text()) != expected:
            raise ValueError("Validated artifact changed after testing")
        if manifest["images"][image]["index_digest"] != graph["index_digest"]:
            raise ValueError("Sources do not describe this exact image")
        plan["images"][image] = {**expected, **graph, "archive": str(archive.resolve()),
                                  "reference": "ghcr.io/flowcool/booksys-" + image + ":" + release["version"]}
    plan["asset_digests"] = {path: file_digest(Path(path)) for path in plan["source_assets"]}
    return plan


def verify_plan(plan: dict[str, Any]) -> None:
    # Publication consumes the same archives and source assets verified during preparation.
    if set(plan["images"]) != set(IMAGES):
        raise ValueError("Incomplete image set")
    for image, entry in plan["images"].items():
        if entry["reference"] != f"ghcr.io/flowcool/booksys-{image}:" + plan["version"]:
            raise ValueError("Unexpected registry target")
        if file_digest(Path(entry["archive"])) != entry["archive_sha256"]:
            raise ValueError("Prepared OCI archive changed")
    for path, expected in plan["asset_digests"].items():
        if file_digest(Path(path)) != expected:
            raise ValueError("Prepared corresponding source changed")
    if plan.get("release_notes") and file_digest(Path(plan["release_notes"])) != plan["release_notes_sha256"]:
        raise ValueError("Prepared release notes changed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    dsc = commands.add_parser("fetch-dsc")
    dsc.add_argument("url")
    dsc.add_argument("sha256")
    dsc.add_argument("destination", type=Path)
    for name in ("assemble", "prepare"):
        command = commands.add_parser(name)
        command.add_argument("release", type=Path)
        command.add_argument("artifacts", type=Path)
        command.add_argument("sources", type=Path)
        if name == "assemble":
            command.add_argument("lock", type=Path)
    relocate = commands.add_parser("relocate")
    relocate.add_argument("directory", type=Path)
    relocate.add_argument("release", type=Path)
    args = parser.parse_args()
    if args.command == "fetch-dsc":
        print(json.dumps(fetch_dsc(args.url, args.sha256, args.destination), indent=2))
    elif args.command == "relocate":
        plan = json.loads((args.directory / "plan.json").read_text())
        current = json.loads(args.release.read_text())
        if any(plan[key] != current[key] for key in ("tag", "version", "revision", "latest", "identity")):
            raise ValueError("Release control plane changed after qualification")
        for image in IMAGES:
            plan["images"][image]["archive"] = str((args.directory / "artifacts" / image / "image.oci.tar").resolve())
        assets = []
        checksums = {}
        for old in plan["source_assets"]:
            path = args.directory / ("sources/manifest.json" if Path(old).name == "manifest.json" else "corresponding-source.tar.gz")
            assets.append(str(path.resolve()))
            checksums[str(path.resolve())] = plan["asset_digests"][old]
        plan["source_assets"] = assets
        plan["asset_digests"] = checksums
        verify_plan(plan)
        print(json.dumps(plan, indent=2))
    else:
        release = json.loads(args.release.read_text())
        result = assemble(json.loads(args.lock.read_text()), args.artifacts, args.sources, release) if args.command == "assemble" else prepare(release, args.artifacts, args.sources)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
