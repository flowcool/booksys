#!/usr/bin/env bash
set -euo pipefail
destination=${1:?usage: install_release_tools.sh DIRECTORY}
mkdir -p "$destination"
# Skopeo 1.18.0; runtime invocation is isolated from host package versions.
cat > "$destination/skopeo" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$RUNNER_TEMP/skopeo-tmp"
exec docker run --rm -i --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --mount "type=bind,source=$RUNNER_TEMP,target=$RUNNER_TEMP" \
  --env REGISTRY_AUTH_FILE --env "TMPDIR=$RUNNER_TEMP/skopeo-tmp" \
  quay.io/skopeo/stable@sha256:c4c8a9d6fc95e331fa92fc31de3f6c9b5fe4761c82f0acb99669eef067fb7c33 \
  --tmpdir "$RUNNER_TEMP/skopeo-tmp" "$@"
SH
chmod +x "$destination/skopeo"
