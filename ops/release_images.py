"""Release-only three-image publication. Never build or replace a version."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

IMAGES = ("worker", "library", "byparr")
REPOSITORY = "flowcool/booksys"
ISSUER = "https://token.actions.githubusercontent.com"


def release_event(event: dict[str, Any], name: str, ref: str, revision: str, main: str, latest_tag: str = "") -> dict[str, Any]:
    release = event.get("release", {})
    tag = release.get("tag_name", "")
    if (name != "release" or event.get("action") != "published"
            or event.get("repository", {}).get("full_name") != REPOSITORY
            or not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag)
            or ref != "refs/tags/" + tag or release.get("draft") is not False
            or type(release.get("prerelease")) is not bool
            or not re.fullmatch(r"[0-9a-f]{40}", revision) or revision != main):
        raise ValueError("Release must be published, numeric vX.Y.Z and resolve to an approved main ancestor")
    if tag == "v0.1.0" and not release["prerelease"]:
        raise ValueError("The initial v0.1.0 must be a GitHub prerelease")
    return {"tag": tag, "version": tag[1:], "revision": revision,
            "latest": not release["prerelease"] and tag == latest_tag,
            "identity": f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@refs/tags/{tag}"}


def run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def registry_manifest(reference: str) -> bytes | None:
    """Only an explicit registry MANIFEST_UNKNOWN means absent; auth/transport fail shut."""
    result = subprocess.run(["skopeo", "inspect", "--raw", "docker://" + reference], capture_output=True)
    if result.returncode == 0:
        return result.stdout
    error = result.stderr.decode(errors="replace")
    if re.search(r"reading manifest [^\n]+: (manifest unknown|name unknown(?:: repository name not known to registry)?)\"?\s*$", error) and not any(word in error.lower() for word in ("unauthorized", "denied", "timeout")):
        return None
    raise RuntimeError("Registry read is uncertain: " + error[:400])


def reconcile(plan: dict[str, Any]) -> dict[str, str]:
    result = {}
    for image in IMAGES:
        entry = plan["images"][image]
        raw = registry_manifest(entry["reference"])
        result[image] = "absent" if raw is None else "match" if digest(raw) == entry["index_digest"] else "conflict"
    return result


def save(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(document, indent=2) + "\n")
    temporary.replace(path)


def publish(plan: dict[str, Any], journal: Path, resume: bool = False) -> None:
    # Recheck the COMPLETE set before any write, including release assets/signatures.
    states = reconcile(plan)
    if "conflict" in states.values() or (not resume and "match" in states.values()):
        raise ValueError("Existing version refused; use digest reconciliation and an approved recovery")
    previous_aliases = {}
    if plan["latest"]:
        latest = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases/latest"))
        if latest["tag_name"] != plan["tag"] or latest["prerelease"] or latest["draft"]:
            raise ValueError("GitHub latest stable release changed; promotion refused")
        for image in IMAGES:
            package = plan["images"][image]["reference"].rsplit(":", 1)[0]
            raw = registry_manifest(package + ":latest")
            previous_aliases[image] = None if raw is None else digest(raw)
            if raw is not None:
                config = json.loads(run("skopeo", "inspect", "--config", "docker://" + package + "@" + digest(raw)))
                version = config.get("config", {}).get("Labels", {}).get("org.opencontainers.image.version", "")
                if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
                    raise ValueError("Existing latest has no trustworthy numeric version")
                if tuple(map(int, version.split("."))) >= tuple(map(int, plan["version"].split("."))):
                    if not (resume and digest(raw) == plan["images"][image]["index_digest"]):
                        raise ValueError("Stable alias cannot move backwards or replace the same version")
    remote_release = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases/tags/{plan['tag']}"))
    notes_digest = digest(remote_release.get("body", "").encode())
    if plan.get("release_notes") and notes_digest not in (
        plan["release_notes_before_sha256"], plan["release_notes_sha256"] if resume else ""
    ):
        raise ValueError("Release notes changed before recovery publication")
    assets = {asset["name"]: asset for asset in remote_release["assets"]}
    existing_assets = set()
    for asset in plan["source_assets"]:
        name = Path(asset).name
        if name in assets:
            if not resume or assets[name].get("digest") != plan["asset_digests"][asset]:
                raise ValueError("Existing/uncertain source asset refused")
            existing_assets.add(name)
    state: dict[str, Any] = {"plan": plan, "preflight": states, "previous_aliases": previous_aliases, "completed": [], "pending": None}
    if journal.exists():
        raise ValueError("Keep the existing journal; choose a new recovery journal")
    save(journal, state)

    def mutate(label: str, *command: str) -> None:
        state["pending"] = label
        save(journal, state)  # Durable before a potentially uncertain write. Never automatic retry.
        try:
            output = run(*command)
        except subprocess.CalledProcessError as error:
            state["failure"] = {"returncode": error.returncode, "stdout": error.stdout, "stderr": error.stderr}
            save(journal, state)
            raise
        state["completed"].append({"step": label, "output": output})
        state["pending"] = None
        save(journal, state)

    if plan.get("release_notes") and notes_digest != plan["release_notes_sha256"]:
        mutate("release-notes", "gh", "release", "edit", plan["tag"], "--repo", REPOSITORY,
               "--notes-file", plan["release_notes"])
        changed = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases/tags/{plan['tag']}"))
        if digest(changed["body"].encode()) != plan["release_notes_sha256"]:
            raise ValueError("Release notes write is uncertain; stop before assets/images")
    for asset in plan["source_assets"]:
        if Path(asset).name in existing_assets:
            continue
        mutate("source:" + Path(asset).name, "gh", "release", "upload", plan["tag"], asset,
               "--repo", REPOSITORY)  # No --clobber. Recovery verifies existing assets separately.
        uploaded = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases/tags/{plan['tag']}"))
        if not any(entry["name"] == Path(asset).name and entry.get("digest") == plan["asset_digests"][asset] for entry in uploaded["assets"]):
            raise ValueError("Source upload is uncertain; images remain unpublished")
    for image in IMAGES:
        entry = plan["images"][image]
        if states[image] == "absent":
            mutate("copy:" + image, "skopeo", "copy", "--all", "--preserve-digests",
                   "oci-archive:" + entry["archive"], "docker://" + entry["reference"])
        verify_graph(entry)
        target = entry["reference"].rsplit(":", 1)[0] + "@" + entry["index_digest"]
        mutate("sign:" + image, "cosign", "sign", "--yes", target)
        certificate = []
        if plan.get("certificate_github_workflow_sha"):
            certificate = ["--certificate-github-workflow-sha", plan["certificate_github_workflow_sha"]]
        verification = run("cosign", "verify", "--certificate-identity", plan["identity"],
                           "--certificate-oidc-issuer", ISSUER, *certificate, target)
        signatures = json.loads(verification)
        if not isinstance(signatures, list) or not signatures:
            raise ValueError("Signature verification returned no verified signature")
        state[image] = {"digest": entry["index_digest"], "signature": signatures}
        save(journal, state)
    # All three immutable versions and signatures must read back before any latest change.
    if reconcile(plan) != dict.fromkeys(IMAGES, "match"):
        raise ValueError("Incomplete release set; stable promotion stopped")
    if plan["latest"]:
        current_release = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases/latest"))
        if current_release["tag_name"] != plan["tag"] or current_release["prerelease"] or current_release["draft"]:
            raise ValueError("Latest release changed during publication; aliases unchanged")
        for image in IMAGES:
            entry = plan["images"][image]
            package = entry["reference"].rsplit(":", 1)[0]
            before = registry_manifest(package + ":latest")
            observed = None if before is None else digest(before)
            if observed != previous_aliases[image]:
                raise ValueError("Stable alias drifted; stop promotion and reconcile")
            if observed == entry["index_digest"]:
                continue
            mutate("latest:" + image, "skopeo", "copy", "--all", "--preserve-digests",
                   "docker://" + package + "@" + entry["index_digest"], "docker://" + package + ":latest")
            raw = registry_manifest(package + ":latest")
            if raw is None or digest(raw) != entry["index_digest"]:
                raise ValueError("Partial stable promotion: stop and reconcile by digest")
    state["complete"] = True
    save(journal, state)


def recovery_report(plan: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {"versions": reconcile(plan), "latest": {}, "signatures": {}, "source_assets": {}}
    for image in IMAGES:
        entry = plan["images"][image]
        package = entry["reference"].rsplit(":", 1)[0]
        raw = registry_manifest(package + ":latest")
        report["latest"][image] = {"observed_digest": None if raw is None else digest(raw), "candidate_digest": entry["index_digest"]}
        if report["versions"][image] == "match":
            try:
                report["signatures"][image] = json.loads(run("cosign", "verify", "--certificate-identity", plan["identity"], "--certificate-oidc-issuer", ISSUER, package + "@" + entry["index_digest"]))
            except subprocess.CalledProcessError as error:
                report["signatures"][image] = {"unverified": error.stderr}
    release = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases/tags/{plan['tag']}"))
    assets = {asset["name"]: asset for asset in release["assets"]}
    for path in plan["source_assets"]:
        asset = assets.get(Path(path).name)
        report["source_assets"][Path(path).name] = "absent" if asset is None else "match" if asset.get("digest") == plan["asset_digests"][path] else "conflict-or-unknown"
    return report


def verify_graph(entry: dict[str, Any]) -> None:
    """Read every index/manifest and blob back; attestations and config are included."""
    package = entry["reference"].rsplit(":", 1)[0]
    raw = registry_manifest(entry["reference"])
    if raw is None or digest(raw) != entry["index_digest"]:
        raise ValueError("Published index differs from tested artifact")
    # A digest-preserving round trip verifies every referenced layer and attestation blob.
    destination = Path(entry["archive"]).with_suffix(".readback.oci.tar")
    if destination.exists():
        raise ValueError("Preserve prior readback; use a fresh artifact directory for recovery")
    run("skopeo", "copy", "--all", "--preserve-digests", "docker://" + package + "@" + entry["index_digest"],
        "oci-archive:" + str(destination))
    from release_sources import oci_graph
    actual = oci_graph(destination)
    if actual["blobs"] != entry["blobs"] or actual["index_digest"] != entry["index_digest"]:
        raise ValueError("Published OCI graph differs from tested graph")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    event = sub.add_parser("event")
    event.add_argument("event", type=Path)
    for argument in ("event-name", "ref", "revision", "main"):
        event.add_argument("--" + argument, required=True)
    event.add_argument("--latest-tag", default="")
    for name in ("publish", "reconcile"):
        command = sub.add_parser(name)
        command.add_argument("plan", type=Path)
        if name == "publish":
            command.add_argument("journal", type=Path)
            command.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.command == "event":
        print(json.dumps(release_event(json.loads(args.event.read_text()), args.event_name, args.ref, args.revision, args.main, args.latest_tag)))
    else:
        plan = json.loads(args.plan.read_text())
        from release_sources import verify_plan
        verify_plan(plan)
        if args.command == "publish":
            publish(plan, args.journal, args.resume)
        else:
            print(json.dumps(recovery_report(plan), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
