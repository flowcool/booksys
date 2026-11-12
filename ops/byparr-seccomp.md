# Byparr validation seccomp profile

Source: Docker Engine 29.9.0, `moby/moby` tag `docker-v29.9.0`, file `vendor/github.com/moby/profiles/seccomp/default.json`.
Source URL: https://github.com/moby/moby/blob/docker-v29.9.0/vendor/github.com/moby/profiles/seccomp/default.json
Source SHA-256: `785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334`.
License: Apache-2.0, retained in `third_party/moby-profiles-LICENSE` from the upstream Moby profiles project.

The profile keeps every upstream rule. It adds one allow entry for `clone`, `setns`, `unshare` and `chroot`. This permits the unprivileged browser to create its own namespace sandbox. It grants no Linux capability and does not disable the Firefox sandbox. The `clone3` fallback and all other restrictions remain unchanged.

GitHub validation uses this profile for the namespace canary and both Byparr probe containers. It retains a read-only root, no network, dropped capabilities and no new privileges. Validation must prove the namespace canary and the actual browser session tests. No local runtime activation is implied.

The `chroot` exception permits Firefox to complete its own chroot-helper handshake inside an unprivileged user namespace. The kernel still requires the capability in the calling namespace. This rule grants no host `CAP_SYS_CHROOT`. Docker's upstream conditional rule depends on initial container capabilities and does not reflect capabilities created later inside a user namespace. Validation requires positive namespace and chroot canaries before the actual native browser tests.

When a container drops all capabilities, use this explicit seccomp profile so Firefox can create its internal namespace and chroot sandbox. A writable runtime root needs no application-directory mount. Read-only validation additionally provides four bounded writable paths: `/cache/invisible-playwright/fonts`, `/cache/fontconfig`, `/home/byparr/.mozilla`, and `/cache/mozilla`. Each uses a 16 MiB tmpfs owned by UID/GID 1000 with `nosuid,nodev,noexec`. The image probe creates `.mozilla/firefox` and `/cache/mozilla/firefox` before loading the tests. Firefox initializes these application directories before the explicit temporary session profile. Keep the rest of HOME read-only so the pinned interpreter remains visible.
