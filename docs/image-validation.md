# Image validation

Validation uses GitHub-hosted runners and read-only repository tokens. It does
not log into a registry, publish an image or access an installation. PRs,
`main` pushes, manual validation and the weekly run use the same image workflow.
Worker, library and Byparr checks run for every change, including documentation.
Later scope filtering must preserve check completion and include renamed paths.

Install the locked dev environment with `uv sync --locked --extra dev`.
The workflow declares Python 3.14. The application minimum remains Python 3.11.
Worker tests use synthetic responses, including loopback HTTP servers.
The library test job explicitly installs Node and native archive/PDF tools.
Missing Node fails the actual JavaScript tests.
No hoster, downloader or real browser session is a test prerequisite.

## Retained artifact boundary

Build one `linux/amd64` OCI archive with a `docker-container` Buildx builder:

```sh
revision=$(git rev-parse HEAD)
docker buildx build --builder validation --platform linux/amd64 \
  --file Dockerfile --label org.opencontainers.image.source=https://github.com/flowcool/booksys \
  --label org.opencontainers.image.revision="$revision" \
  --output type=oci,dest=tmp/worker.oci.tar .
bash ops/install_validation_tools.sh tmp/validation-tools
PATH="$PWDEVIDENCE_DIR" bash ops/validate_image.sh \
  worker tmp/worker.oci.tar "$revision" tmp/worker-proof
```

Create the local builder first with
`docker buildx create --name validation --driver docker-container`.
Local prerequisites are Docker 29.9 with its containerd image store and Buildx,
Python 3.11+, curl, tar and sha256sum. CI sets up the same Docker version and
containerd image store explicitly. Tool downloads have fixed versions
and SHA256 checks; update both together against the upstream release.

The helper verifies source labels and the executable OCI config digest. Docker
loads the OCI archive directly. The loaded manifest digest must equal the
retained manifest digest before a probe can start. The original
archive remains intact, including available attestations. A later release can
promote that archive without rebuilding. The daemon copy is only a probe input.

The probe has no external network, host mounts, capabilities or writable root
filesystem. It uses bounded memory, process count, CPU and a temporary filesystem.
It checks the default CLI help, real collector CLI output, synthetic persisted
links, restart behavior and solver-failure behavior through a loopback API.
It does not submit a download.

## Security proof

Trivy scans the extracted OCI layout. It does not accept OCI tarballs as input.
The scan image ID must match the OCI config digest. No conversion or rebuild
occurs. `vulnerabilities.json` retains all known
findings, including unfixed findings. `secrets.json` retains the secret scan.
The gate blocks every secret finding and HIGH/CRITICAL vulnerability with an
available fix. Scanner failures also fail validation. No repository ignore file
or blanket exception is consumed. Any exception requires a separate approved
finding, exact digest, rationale, expiry and follow-up.

`identity.json` binds these reports to the archive checksum, executable manifest,
config digest, platform and source revision. Docker 29.9 with containerd reports
the manifest digest as its image `Id`; Trivy reports the config digest as
`Metadata.ImageID`. The helper checks both against the original OCI archive.
Docker-archive conversion can change the manifest digest, so it is not used.
Logs and image inspection are retained
with the reports, including on a gate failure. CI retains reports for 14 days;
release proof needs its own durable retention. Local proof belongs in ignored
`tmp/` or an external evidence directory. Containers use `--rm`; evidence is in
files. Image artifacts and retained proof are not automatically removed.

GitHub workflow execution and registry publication require separate observed
evidence. Local validation proves only the recorded local artifact.

References: [Docker OCI exporter](https://docs.docker.com/build/exporters/oci-docker/),
[Docker attestations](https://docs.docker.com/build/metadata/attestations/),
[GitHub secure Actions](https://docs.github.com/en/actions/reference/security/secure-use),
[Trivy image inputs](https://trivy.dev/docs/latest/target/container_image/).

## Library image inputs

Use the same build commands with `Dockerfile.library` and distinct archive paths.
The image keeps its pinned Python 3.14 base.
The five Dockerfiles use local source paths from this tree.
Existing image probes have not been qualified for the snapshot of 13 November 2026.
A successful source-path check does not establish image qualification.

## Byparr adapter image

Use the same build and validation commands with `Dockerfile.byparr`,
`byparr` and distinct archive/report paths. Keep the pinned upstream base,
Python 3.14 interpreter, uid 1000 and existing `tini` adapter entrypoint.
The adapter uses the pinned base dependencies. The probe uses their actual
FastAPI and upstream request code.

The adapter is a server. It has no CLI help interface. The common helper starts
its actual entrypoint under the same network-disabled, read-only restrictions.
`tests/probes/byparr.py` checks `tini`, polls loopback `/health`, submits an
invalid external-origin input and checks that session generation remains zero.
It rejects browser processes. Health readiness means the adapter accepts requests;
it does not prove a browser can navigate or solve a challenge.
Logs and container inspection are saved before the owned server stops.
The readiness container has no automatic removal on exit, so startup failures
keep their logs. Cleanup checks each result and always attempts the owned stop.
It removes the container only after evidence capture and stop succeed. Otherwise
it retains the container for recovery and fails validation. The original probe
failure remains the exit result. Command fixtures verify these failure paths.

A second isolated container loads the delivered `/app/session_api.py` and streams
`tests/test_byparr_session.py` into unittest. Missing upstream dependencies,
empty suites, skipped cases or failures fail validation. Browser factories and
page responses are synthetic. The tests exercise the real pinned upstream
request path with mocked pages/challenge results. They cover reuse, timeouts,
solver failure without retry, invalid HTML and origin refusal, busy responses,
request/age/idle/memory recycling, startup recovery, shutdown cleanup and failed
cleanup readiness. No browser process, real session or external navigation is
required. Tests and probe code are inputs, not extra runtime image contents.
Full image scans and exact-artifact identity checks use the shared mechanism.
