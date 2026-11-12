"""Publish the retained v0.1.0 graphs with two bounded source assets. Never rebuild."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

from release_images import publish, run
from release_sources import file_digest, prepare, verify_plan

REVISION = "8e77af5582fd9ab55c8b48536548288a1235a54c"
RUN = 43171690522
ARTIFACT = 13924175975
ARTIFACT_DIGEST = "sha256:a0a345d448910cbc6d27e366421d3e709f73688d5031555d7016383e27645f4c"
WHOLE = "sha256:b683a7ffcd6233db3050c54e5f49c871db89251e37ed89ffb435cac8678ce189"
MANIFEST = "sha256:05ea6eff52260b457e47ca8fb58f3ab6c5b2d17c5fc28229a27db052f49a6ab0"
IDENTITY = "https://github.com/flowcool/booksys/.github/workflows/release-recovery.yml@refs/heads/main"
DIGESTS = {
    "worker": "sha256:a2d8f8f8efdcc821c9fac98620082a3cf1ead133d0bf933a45899cd95ddf28f9",
    "library": "sha256:156b4db47d15f912f9bb7145b60d22caef7270182d0ee62eaa1e0c198950b007",
    "byparr": "sha256:b8638c5eb4c9e72a3bbb521bdb2a1c750ef283db38c458caaaf249e220fd0dfb",
}
BASE_URL = "https://github.com/flowcool/booksys/releases/download/v0.1.0/"


def control() -> tuple[dict[str, Any], str]:
    if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_REPOSITORY") != "flowcool/booksys"):
        raise ValueError("Recovery requires a trusted dispatch on protected main")
    sha = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or run("git", "rev-parse", "HEAD").strip() != sha:
        raise ValueError("Recovery control revision changed")
    main = json.loads(run("gh", "api", "repos/flowcool/booksys/commits/main"))
    if main["sha"] != sha:
        raise ValueError("Protected main changed after dispatch")
    run("git", "merge-base", "--is-ancestor", REVISION, sha)
    original = json.loads(run("gh", "api", f"repos/flowcool/booksys/actions/runs/{RUN}"))
    artifact = json.loads(run("gh", "api", f"repos/flowcool/booksys/actions/artifacts/{ARTIFACT}"))
    if (original["head_sha"] != REVISION or original["event"] != "release"
            or artifact["workflow_run"]["id"] != RUN or artifact["digest"] != ARTIFACT_DIGEST
            or artifact["name"] != "qualified-release" or artifact["expired"]):
        raise ValueError("Original qualified artifact identity changed")
    release = json.loads(run("gh", "api", "repos/flowcool/booksys/releases/tags/v0.1.0"))
    tag = json.loads(run("gh", "api", "repos/flowcool/booksys/git/ref/tags/v0.1.0"))
    if (release["draft"] or not release["prerelease"] or release["tag_name"] != "v0.1.0"
            or release["target_commitish"] != REVISION or tag["object"]["sha"] != REVISION):
        raise ValueError("Release or source tag changed")
    return release, sha


def split_source(archive: Path, destination: Path, expected: str, part_size: int = 1792 * 1024 * 1024) -> dict[str, Any]:
    whole = hashlib.sha256()
    parts = []
    with archive.open("rb") as source:
        index = 0
        while chunk := source.read(min(part_size, 1024 * 1024)):
            name = archive.name + f".part-{index:03d}"
            checksum = hashlib.sha256()
            size = 0
            with (destination / name).open("xb") as output:
                while chunk:
                    whole.update(chunk)
                    checksum.update(chunk)
                    output.write(chunk)
                    size += len(chunk)
                    if size == part_size:
                        break
                    chunk = source.read(min(part_size - size, 1024 * 1024))
            parts.append({"name": name, "size": size, "sha256": "sha256:" + checksum.hexdigest(), "url": BASE_URL + name})
            index += 1
    if "sha256:" + whole.hexdigest() != expected:
        raise ValueError("Retained whole source archive differs; keep failed parts for inspection")
    return {"name": archive.name, "size": archive.stat().st_size, "sha256": expected, "parts": parts,
            "reassemble": "cat corresponding-source.tar.gz.part-000 corresponding-source.tar.gz.part-001 > corresponding-source.tar.gz"}


def prepare_recovery(source: Path, destination: Path) -> None:
    remote, control_sha = control()
    original = json.loads((source / "plan.json").read_text())
    expected = {"tag": "v0.1.0", "version": "0.1.0", "revision": REVISION, "latest": False,
                "identity": "https://github.com/flowcool/booksys/.github/workflows/release.yml@refs/tags/v0.1.0"}
    if any(original[key] != value for key, value in expected.items()):
        raise ValueError("Original release binding changed")
    if file_digest(source / "corresponding-source.tar.gz") != WHOLE or file_digest(source / "sources/manifest.json") != MANIFEST:
        raise ValueError("Original source assets changed")
    # Reuse the established exact graph, scan, label and attestation qualification.
    plan = prepare(expected, source / "artifacts", source / "sources")
    if {name: entry["index_digest"] for name, entry in plan["images"].items()} != DIGESTS:
        raise ValueError("Recovery cannot change any original tested graph")
    if any(entry["archive_sha256"] != original["images"][name]["archive_sha256"] for name, entry in plan["images"].items()):
        raise ValueError("Recovery cannot change any original OCI archive bytes")
    destination.mkdir(parents=True, exist_ok=False)
    archive = split_source(source / "corresponding-source.tar.gz", destination, WHOLE)
    if len(archive["parts"]) != 2 or any(part["size"] >= 2 * 1024**3 for part in archive["parts"]):
        raise ValueError("Expected exactly two source parts below the hosting limit")
    manifest = json.loads((source / "sources/manifest.json").read_text())
    if len(manifest["sources"]) != 342:
        raise ValueError("Preserve the complete qualified source ledger")
    manifest["source_archive"] = archive
    manifest["original_manifest_sha256"] = MANIFEST
    (destination / "sources").mkdir()
    (destination / "sources/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.move(str(source / "artifacts"), destination / "artifacts")
    for name in plan["images"]:
        plan["images"][name]["archive"] = str((destination / "artifacts" / name / "image.oci.tar").resolve())
    plan["source_assets"] = [str((destination / part["name"]).resolve()) for part in archive["parts"]] + [str((destination / "sources/manifest.json").resolve())]
    plan["asset_digests"] = {name: file_digest(Path(name)) for name in plan["source_assets"]}
    plan["identity"] = IDENTITY
    plan["certificate_github_workflow_sha"] = control_sha
    body = remote["body"]
    old = "- [Corresponding source and notices](" + BASE_URL + "corresponding-source.tar.gz)"
    if old not in body:
        raise ValueError("Release notes changed before multipart recovery")
    replacement = "\n".join(f"- [Corresponding source part {index + 1} of 2]({part['url']})" for index, part in enumerate(archive["parts"]))
    body = body.replace(old, replacement) + "\n\nReassemble the two parts in order with `" + archive["reassemble"] + "`. Verify the whole archive SHA256: `" + WHOLE[7:] + "`. The manifest records each part's size and SHA256.\n"
    notes = destination / "release-notes.md"
    notes.write_text(body)
    plan["release_notes"] = str(notes.resolve())
    plan["release_notes_before_sha256"] = "sha256:" + hashlib.sha256(remote["body"].encode()).hexdigest()
    plan["release_notes_sha256"] = file_digest(notes)
    verify_plan(plan)
    (destination / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preparation = sub.add_parser("prepare")
    preparation.add_argument("source", type=Path)
    preparation.add_argument("destination", type=Path)
    publication = sub.add_parser("publish")
    publication.add_argument("directory", type=Path)
    publication.add_argument("journal", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare_recovery(args.source, args.destination)
        return
    _, control_sha = control()
    plan = json.loads((args.directory / "plan.json").read_text())
    if (plan["tag"] != "v0.1.0" or plan["version"] != "0.1.0" or plan["latest"] is not False
            or plan["revision"] != REVISION or plan["identity"] != IDENTITY
            or plan["certificate_github_workflow_sha"] != control_sha
            or {name: item["index_digest"] for name, item in plan["images"].items()} != DIGESTS):
        raise ValueError("Recovery publication binding changed")
    for name, entry in plan["images"].items():
        entry["archive"] = str((args.directory / "artifacts" / name / "image.oci.tar").resolve())
    digests = {Path(path).name: checksum for path, checksum in plan["asset_digests"].items()}
    names = {"corresponding-source.tar.gz.part-000", "corresponding-source.tar.gz.part-001", "manifest.json"}
    if set(digests) != names:
        raise ValueError("Unexpected recovery source assets")
    plan["source_assets"] = [str((args.directory / ("sources/" + name if name == "manifest.json" else name)).resolve()) for name in digests]
    plan["asset_digests"] = {path: digests[Path(path).name] for path in plan["source_assets"]}
    plan["release_notes"] = str((args.directory / "release-notes.md").resolve())
    verify_plan(plan)
    publish(plan, args.journal, resume=int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")) > 1)


if __name__ == "__main__":
    main()
