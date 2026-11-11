# Byparr validation seccomp profile

Source: Docker Engine 29.9.0, `moby/moby` tag `docker-v29.9.0`, file `vendor/github.com/moby/profiles/seccomp/default.json`.
Source URL: https://github.com/moby/moby/blob/docker-v29.9.0/vendor/github.com/moby/profiles/seccomp/default.json
Source SHA-256: `785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334`.
License: Apache-2.0, retained in `third_party/moby-profiles-LICENSE` from the upstream Moby profiles project.

The profile keeps every upstream rule. It adds one allow entry for `clone`, `setns` and `unshare`. This permits the unprivileged browser to create its own namespace sandbox. It grants no Linux capability and does not disable the Firefox sandbox. The `clone3` fallback and all other restrictions remain unchanged.

GitHub validation uses this profile for the namespace canary and both Byparr probe containers. It retains a read-only root, no network, dropped capabilities and no new privileges. Validation must prove the namespace canary and the actual browser session tests. No local runtime activation is implied.
