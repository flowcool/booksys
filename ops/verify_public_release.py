"""Read public release assets without credentials and retain bounded proof."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from image_validation import identity
from release_sources import oci_graph


def verify_image(archive: Path, revision: str, expected: str) -> dict[str, str]:
    actual = identity(archive, revision)
    graph = oci_graph(archive)
    if graph["index_digest"] != expected or actual["index_digest"] != expected:
        raise ValueError("Anonymous graph differs from the qualified digest")
    with tarfile.open(archive) as stream:
        config = stream.extractfile("blobs/sha256/" + actual["config_digest"][7:])
        assert config is not None
        labels = json.load(config)["config"]["Labels"]
    if labels.get("org.opencontainers.image.version") != "0.1.0":
        raise ValueError("Unexpected image version label")
    types = set()
    for item in graph["statements"]:
        statement = item["statement"]
        if item["runtime_digest"] != actual["manifest_digest"] or not any(
            subject.get("digest", {}).get("sha256") == actual["manifest_digest"][7:]
            for subject in statement.get("subject", [])
        ):
            raise ValueError("Public attestation does not bind the pulled runtime")
        kind = statement.get("predicateType")
        types.add(kind)
        if kind == "https://slsa.dev/provenance/v1":
            definition = statement["predicate"]["buildDefinition"]
            if not definition.get("internalParameters", {}).get("buildConfig"):
                raise ValueError("Maximum provenance is missing")
            args = definition["externalParameters"]["request"]["args"]
            if any(args.get("label:" + key) != labels[key] for key in (
                "org.opencontainers.image.source", "org.opencontainers.image.revision", "org.opencontainers.image.version"
            )):
                raise ValueError("Public provenance source labels differ")
    if not {"https://spdx.dev/Document", "https://slsa.dev/provenance/v1"} <= types:
        raise ValueError("Public SBOM/provenance is missing")
    return actual


def download(url: str, expected: str, retain: bool = False) -> tuple[bytes, int]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise ValueError("An exact qualified SHA256 is required")
    checksum = hashlib.sha256()
    size = 0
    content = bytearray()
    with urlopen(Request(url, headers={"User-Agent": "booksys-public-release-proof"}), timeout=120) as response:
        if urlparse(response.url).scheme != "https":
            raise ValueError("Asset redirected outside HTTPS")
        while chunk := response.read(1024 * 1024):
            checksum.update(chunk)
            size += len(chunk)
            if retain:
                if size > 10 * 1024 * 1024:
                    raise ValueError("Manifest exceeds the bounded JSON size")
                content.extend(chunk)
    if "sha256:" + checksum.hexdigest() != expected:
        raise ValueError("Anonymous asset differs from the qualified release")
    return bytes(content), size


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-only", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        raise ValueError("An exact source revision is required")
    # No authenticated client or GitHub token is used.
    request = Request("https://api.github.com/repos/flowcool/booksys/releases/tags/v0.1.0",
                      headers={"User-Agent": "booksys-public-release-proof", "Accept": "application/vnd.github+json"})
    with urlopen(request, timeout=60) as response:
        release = json.load(response)
    if (release["tag_name"] != "v0.1.0" or release["draft"] or not release["prerelease"]
            or release["target_commitish"] != args.revision):
        raise ValueError("Unexpected release control plane")
    assets = {item["name"]: item for item in release["assets"]}
    proof: dict[str, Any] = {"release": release["html_url"], "revision": args.revision, "authenticated": False, "assets": {}}
    args.output.mkdir(parents=True, exist_ok=False)
    for name, expected in (("manifest.json", args.manifest_sha256),
                           ("corresponding-source.tar.gz", args.source_sha256)):
        asset = assets[name]
        if asset.get("digest") != expected:
            raise ValueError("Release asset digest differs from the qualified plan")
        if args.manifest_only and name != "manifest.json":
            continue
        content, size = download(asset["browser_download_url"], expected, name == "manifest.json")
        proof["assets"][name] = {"url": asset["browser_download_url"], "sha256": expected, "size": size}
        if name == "manifest.json":
            manifest = json.loads(content)
            if manifest["revision"] != args.revision or set(manifest["images"]) != {"worker", "library", "byparr"}:
                raise ValueError("Unexpected corresponding-source manifest")
            (args.output / name).write_bytes(content)
    (args.output / "anonymous-source-proof.json").write_text(json.dumps(proof, indent=2) + "\n")


if __name__ == "__main__":
    main()
