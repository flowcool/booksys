"""Compare isolated Byparr images on a bounded Bookys sample; never call pyLoad."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from booksys import DEFAULT_CATEGORY, catalogue, document, extract_page

ITEM = "https://www7.bookys-ebooks.com/bandes-dessinees/358549-la-sorciere-rouge-int-la-derniere-porte-2026"
URLS = [DEFAULT_CATEGORY, ITEM, ITEM + "/dl/2435952", ITEM + "/dl/2435954"]


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def run_sample(image: str, mode: str, output: Path, repeat: int) -> dict[str, Any]:
    name = "booksys-session-probe-" + uuid.uuid4().hex[:12]
    image_id = docker("image", "inspect", image, "--format", "{{.Id}}")
    rows: list[dict[str, Any]] = []
    docker("run", "--detach", "--rm", "--name", name, "--label", "booksys.purpose=session-probe",
           "--memory", "1536m", "--cpus", "1.5", "--shm-size", "512m",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges", image_id)
    try:
        address = docker("inspect", name, "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")
        endpoint = "http://" + address + ":8191"
        deadline = time.monotonic() + 60
        while True:
            try:
                with urlopen(endpoint + "/docs", timeout=2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Isolated Byparr did not become ready") from None
                time.sleep(1)
        for index, url in enumerate(URLS * repeat):
            started = time.monotonic()
            request = Request(endpoint + "/v1", json.dumps({"cmd": "request.get", "url": url,
                "maxTimeout": 60000, "blockMedia": False}).encode(), {"Content-Type": "application/json"})
            with urlopen(request, timeout=80) as response:
                result = json.load(response)
            solution = result["solution"]
            if result["status"] != "ok" or solution["status"] != 200 or solution["url"] != url:
                raise RuntimeError("Unexpected Byparr response; stop without retry")
            html = solution["response"]
            soup = document(html)
            if url == DEFAULT_CATEGORY:
                items, _ = catalogue(html, url)
                proof = {"catalogue_items": len(items)}
            elif url == ITEM:
                proof = {"metadata_fields": len(extract_page(html, url))}
            else:
                links = soup.select("a.bys-link.bys-host[href]")
                if not links:
                    raise RuntimeError("Missing outgoing anchors; stop without following them")
                proof = {"outgoing_anchors": len(links)}
            rows.append({"index": index, "url": url, "seconds": round(time.monotonic() - started, 3),
                "proof": proof, "memory_bytes": int(docker("exec", name, "cat", "/sys/fs/cgroup/memory.current"))})
            (output / (mode + ".json")).write_text(json.dumps(rows, indent=2))
            print(json.dumps({"mode": mode, **rows[-1]}), flush=True)
            time.sleep(2)
        return {"image_id": image_id, "requests": len(rows), "seconds": round(sum(r["seconds"] for r in rows), 3),
                "memory_samples": [r["memory_bytes"] for r in rows]}
    finally:
        # Docker writes application logs to stderr too.
        captured = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=True)
        logs = captured.stdout + captured.stderr
        (output / (mode + ".log")).write_text(logs)
        (output / (mode + "-challenges.json")).write_text(json.dumps({
            "challenges": logs.count("Challenge detected, waiting for it to clear...")}))
        docker("stop", "--timeout", "10", name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeat", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--reuse-baseline", type=Path, help="Reuse an unchanged fresh-image comparison")
    args = parser.parse_args()
    if docker("info", "--format", "{{.Name}}") != "booksys-host":
        raise RuntimeError("Expected booksys-host")
    args.output.mkdir(parents=True, exist_ok=False)
    base = (ROOT / "Dockerfile.byparr").read_text().splitlines()[0].split()[1]
    if args.reuse_baseline:
        baseline = json.loads((args.reuse_baseline / "summary.json").read_text())["fresh"]
        rows = json.loads((args.reuse_baseline / "fresh.json").read_text())
        if (baseline["image_id"] != docker("image", "inspect", base, "--format", "{{.Id}}")
                or [row["url"] for row in rows] != URLS):
            raise RuntimeError("Baseline image or inputs differ")
        for name in ["fresh.json", "fresh.log", "fresh-challenges.json"]:
            shutil.copy2(args.reuse_baseline / name, args.output / name)
    else:
        baseline = run_sample(base, "fresh", args.output, 1)
    result = {"fresh": baseline,
              "persistent": run_sample(args.image, "persistent", args.output, args.repeat),
              "baseline_source": str(args.reuse_baseline) if args.reuse_baseline else None,
              "limits": ["Small fixed sample; sequential arms; no statistical or long-run guarantee.",
                         "Durations include browser startup where applicable, and exclude two-second pacing.",
                         "Memory is cgroup memory.current, including cache. No cookies or HTML stored."]}
    (args.output / "summary.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
