#!/usr/bin/env bash
# Validate the retained OCI archive. No registry login or publication occurs.
set -euo pipefail
image=${1:?usage: validate_image.sh IMAGE OCI_ARCHIVE REVISION REPORT_DIRECTORY}
archive=$(realpath "${2:?}")
revision=${3:?}
reports=${4:?}
case "$image" in
  worker|library|byparr) interpreter=/app/.venv/bin/python ;;
  *) echo "Unsupported image: $image" >&2; exit 2 ;;
esac
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || exit 2
mkdir -p "$reports"
reports=$(realpath "$reports")
python3 ops/image_validation.py identity "$archive" "$revision" > "$reports/identity.json"
# Containerd's image store loads OCI directly and preserves its root digest.
# A Docker-archive conversion changes that digest, so it is not a probe input.
docker load --input "$archive" > "$reports/load.log" 2>&1
image_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["index_digest"])' "$reports/identity.json")
loaded_id=$(docker image inspect --format '{{.Id}}' "$image_id")
[[ "$loaded_id" == "$image_id" ]] || { echo "Loaded image differs from retained OCI artifact" >&2; exit 1; }
docker image inspect "$image_id" > "$reports/inspect.json"
if [[ "$image" == byparr ]]; then
  # Require the namespace and chroot operations used by the Firefox sandbox.
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --security-opt "seccomp=$PWD/ops/byparr-seccomp.json" --entrypoint /usr/bin/unshare "$image_id" -Ur /bin/true > "$reports/namespace-profile.log" 2>&1
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --env LC_ALL=C --security-opt "seccomp=$PWD/ops/byparr-seccomp.json" --entrypoint /usr/bin/unshare \
    "$image_id" -Ur "$interpreter" -c "import os; os.chroot('/'); os.execv('/bin/true', ['/bin/true'])" > "$reports/chroot-profile.log" 2>&1
  # Require writable application directories before the explicit session profile.
  docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --security-opt "seccomp=$PWD/ops/byparr-seccomp.json" \
    --tmpfs /home/byparr/.mozilla:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 \
    --tmpfs /cache/mozilla:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 \
    --entrypoint "$interpreter" "$image_id" - > "$reports/appdata-profile.log" 2>&1 <<'PYAPPDATA'
from pathlib import Path
for name in ('/home/byparr/.mozilla/firefox', '/cache/mozilla/firefox'):
    Path(name).mkdir(parents=True, exist_ok=True)
    print({'path': name, 'created': True}, flush=True)
PYAPPDATA
  # The adapter is a server: --help would start it. Probe its actual tini entrypoint.
  container=$(docker run -d --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --security-opt "seccomp=$PWD/ops/byparr-seccomp.json" --pids-limit 1024 --memory 1536m --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --tmpfs /cache/invisible-playwright/fonts:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 \
    --tmpfs /cache/fontconfig:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 \
    --tmpfs /home/byparr/.mozilla:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 \
    --tmpfs /cache/mozilla:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 "$image_id")
  finish_readiness() {
    local status=0 command_status
    # Keep an exited container until evidence is saved. Always attempt its stop.
    if docker logs "$container" > "$reports/entrypoint.log" 2>&1; then :; else
      command_status=$?; echo "Readiness logs failed: exit $command_status" >&2; status=1
    fi
    if docker inspect "$container" > "$reports/container-inspect.json"; then :; else
      command_status=$?; echo "Readiness inspection failed: exit $command_status" >&2; status=1
    fi
    if docker stop --time 10 "$container" > "$reports/stop.log" 2>&1; then :; else
      command_status=$?; echo "Readiness stop failed: exit $command_status" >&2; status=1
    fi
    if (( status == 0 )); then
      if docker rm "$container" > "$reports/remove.log" 2>&1; then :; else
        command_status=$?; echo "Readiness removal failed: exit $command_status" >&2; status=1
      fi
    else
      echo "Retained owned readiness container for recovery: $container" >&2
    fi
    return "$status"
  }
  readiness_exit() {
    local original_status=$? cleanup_status
    trap - EXIT
    if finish_readiness; then cleanup_status=0; else cleanup_status=$?; fi
    if (( original_status != 0 )); then exit "$original_status"; fi
    exit "$cleanup_status"
  }
  trap readiness_exit EXIT
  docker exec -i "$container" "$interpreter" - readiness < tests/probes/byparr.py > "$reports/readiness.log" 2>&1
  if finish_readiness; then
    trap - EXIT
  else
    trap - EXIT
    exit 1
  fi
  docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --security-opt "seccomp=$PWD/ops/byparr-seccomp.json" --pids-limit 1024 --memory 1536m --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --tmpfs /cache/invisible-playwright/fonts:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 \
    --tmpfs /cache/fontconfig:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 \
    --tmpfs /home/byparr/.mozilla:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 \
    --tmpfs /cache/mozilla:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000 \
    --env BEADS_DIR=/tmp/nonexistent-test-beads --entrypoint "$interpreter" "$image_id" -c "$(cat tests/probes/byparr.py)" sessions \
    < tests/test_byparr_session.py > "$reports/probe.log" 2>&1
else
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 128 --memory 512m --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    "$image_id" --help > "$reports/cli.log" 2>&1
  if [[ "$image" == worker ]]; then
    docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
      --pids-limit 128 --memory 512m --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=64m \
      --entrypoint "$interpreter" "$image_id" -c "$(cat tests/probes/worker.py)" \
      < tests/test_provider_spacing.py > "$reports/probe.log" 2>&1
  else
    docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
      --pids-limit 128 --memory 512m --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=64m \
      --entrypoint "$interpreter" "$image_id" - < "tests/probes/$image.py" > "$reports/probe.log" 2>&1
  fi
fi
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
