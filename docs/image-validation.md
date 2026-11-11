# Image validation

Validation uses GitHub-hosted runners and read-only repository tokens. It does
not log into a registry, publish an image or access an installation. PRs,
`main` pushes, manual validation and the weekly run use the same image workflow.
This first slice runs worker checks for every change, including documentation.
Later scope filtering must preserve check completion and include renamed paths.

Install the locked dev environment with `uv sync --locked --extra dev`.
The workflow declares Python 3.11, which is the existing minimum; it does not
change the runtime interpreter or the application's Python declaration.
Worker tests use synthetic responses, including loopback HTTP servers.
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

The worker retains its inherited interpreter. Its Ubuntu `apt` and `libapt-pkg7.0`
packages are updated from `3.2.0` to `3.2.1ubuntu0.1` for the known severe finding.
Both inherited pip installations are build tools. They are removed after the
worker dependencies are installed, including their vendored vulnerable packages.
No vulnerability exception or scan exclusion is used.

References: [Docker OCI exporter](https://docs.docker.com/build/exporters/oci-docker/),
[Docker attestations](https://docs.docker.com/build/metadata/attestations/),
[GitHub secure Actions](https://docs.github.com/en/actions/reference/security/secure-use),
[Trivy image inputs](https://trivy.dev/docs/latest/target/container_image/).
