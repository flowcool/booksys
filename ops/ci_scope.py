"""Choose affected validation slices from complete local Git event diffs."""
from __future__ import annotations
import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

SLICES = ("worker", "library", "byparr")
SHA = re.compile(r"[0-9a-f]{40,64}\Z")


def changed_paths(event_name: str, event: dict[str, Any]) -> list[str] | None:
    """Include both rename paths; missing history or unknown events select all."""
    try:
        if event_name == "pull_request":
            pr = event["pull_request"]
            assert isinstance(pr, dict)
            base, head = pr["base"]["sha"], pr["head"]["sha"]
            separator = "..."
        elif event_name == "push":
            base, head = event["before"], event["after"]
            separator = ".."
        else:
            return None
        if not all(isinstance(x, str) and SHA.fullmatch(x) and set(x) != {"0"} for x in (base, head)):
            return None
        diff = subprocess.check_output([
            "git", "diff", "--name-status", "-z", "--find-renames", f"{base}{separator}{head}", "--"
        ], stderr=subprocess.PIPE)
        fields = iter(diff.decode("utf-8").split("\0")[:-1])
        paths = []
        for status in fields:
            paths.append(next(fields))
            if status.startswith(("R", "C")):
                paths.append(next(fields))
        return paths
    except (KeyError, TypeError, AssertionError, StopIteration, UnicodeError, subprocess.CalledProcessError):
        return None


def scope(paths: list[str] | None) -> dict[str, bool]:
    if paths is None or not paths:
        return dict.fromkeys(SLICES, True)
    selected = dict.fromkeys(SLICES, False)
    for path in paths:
        if path == "Dockerfile":
            selected["worker"] = True
        elif path == "Dockerfile.library" or path.startswith("library/"):
            selected["library"] = True
        elif path == "Dockerfile.byparr" or path.startswith("ops/byparr/"):
            selected["byparr"] = True
        elif path.startswith("tests/test_library_") or path.startswith("tests/fixtures/french-metadata/"):
            selected["library"] = True
        elif path.startswith("docs/") or path.endswith(".md") or path == "LICENSE" or path.startswith(".github/ISSUE_TEMPLATE/"):
            continue
        else:
            return dict.fromkeys(SLICES, True)
    return selected


def require_results(results: dict[str, Any]) -> None:
    """Accept skips only for slices that the successful scope job excluded."""
    for name in ("scope", "policy"):
        if results.get(name, {}).get("result") != "success":
            raise SystemExit(f"{name} did not succeed")
    selected = results["scope"].get("outputs", {})
    if any(selected.get(name) not in ("true", "false") for name in SLICES):
        raise SystemExit("Incomplete scope outputs")
    expected = {f"{name}-image": selected[name] == "true" for name in SLICES}
    expected["offline-tests"] = any(value == "true" for value in selected.values())
    for name, enabled in expected.items():
        wanted = "success" if enabled else "skipped"
        if results.get(name, {}).get("result") != wanted:
            raise SystemExit(f"{name} must be {wanted}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-results")
    parser.add_argument("event_name", nargs="?")
    parser.add_argument("event_path", type=Path, nargs="?")
    parser.add_argument("output", type=Path, nargs="?")
    args = parser.parse_args()
    if args.check_results is not None:
        require_results(json.loads(args.check_results))
        return
    if args.event_name is None or args.event_path is None or args.output is None:
        parser.error("event_name, event_path and output are required")
    try:
        paths = changed_paths(args.event_name, json.loads(args.event_path.read_text()))
    except (OSError, ValueError):
        paths = None
    with args.output.open("a") as output:
        for name, enabled in scope(paths).items():
            print(f"{name}={str(enabled).lower()}", file=output)


if __name__ == "__main__":
    main()
