#!/usr/bin/env bash
# Validate the retained OCI archive. No registry login or publication occurs.
set -euo pipefail
image=${1:?usage: validate_image.sh IMAGE OCI_ARCHIVE REVISION REPORT_DIRECTORY}
archive=$(realpath "${2:?}")
revision=${3:?}
reports=${4:?}
case "$image" in
  worker|library) interpreter=/app/.venv/bin/python ;;
  *) echo "Unsupported image: $image" >&2; exit 2 ;;
esac
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || exit 2
mkdir -p "$reports"
reports=$(realpath "$reports")
python3 ops/image_validation.py identity "$archive" "$revision" > "$reports/identity.json"
# Containerd's image store loads OCI directly and preserves its manifest digest.
# A Docker-archive conversion changes that digest, so it is not a probe input.
docker load --input "$archive" > "$reports/load.log" 2>&1
image_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifest_digest"])' "$reports/identity.json")
loaded_id=$(docker image inspect --format '{{.Id}}' "$image_id")
[[ "$loaded_id" == "$image_id" ]] || { echo "Loaded image differs from retained OCI artifact" >&2; exit 1; }
docker image inspect "$image_id" > "$reports/inspect.json"
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 128 --memory 512m --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  "$image_id" --help > "$reports/cli.log" 2>&1
docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 128 --memory 512m --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --entrypoint "$interpreter" "$image_id" - < "tests/probes/$image.py" > "$reports/probe.log" 2>&1
trivy --version > "$reports/trivy-version.txt"
# Trivy accepts OCI layout directories, but not OCI tarballs. This archive is
# produced by BuildKit; its members are OCI paths, not paths from the image.
scan_archive="${archive%.tar}.layout"
mkdir -p "$scan_archive"
tar -xf "$archive" -C "$scan_archive"
# Capture complete findings, including unfixable vulnerabilities, before gating.
# Never consume repository ignore files or silently accept scanner failures.
trivy image --input "$scan_archive" --platform linux/amd64 --scanners vuln --ignorefile /dev/null \
  --format json --output "$reports/vulnerabilities.json" --timeout 10m > "$reports/vulnerabilities.log" 2>&1
trivy image --input "$scan_archive" --platform linux/amd64 --scanners secret --ignorefile /dev/null \
  --format json --output "$reports/secrets.json" --timeout 10m > "$reports/secrets.log" 2>&1
python3 ops/image_validation.py gate "$reports/identity.json" "$reports/vulnerabilities.json" "$reports/secrets.json" > "$reports/security-gate.json"
