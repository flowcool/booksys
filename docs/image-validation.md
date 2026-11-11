# Image validation

Validation uses GitHub-hosted runners and read-only repository tokens. It does
not log into a registry, publish an image or access an installation. PRs,
`main` pushes, manual validation and the weekly run use the same image workflow.
Worker and library checks run for every change, including documentation.
Later scope filtering must preserve check completion and include renamed paths.

Install the locked dev environment with `uv sync --locked --extra dev`.
The workflow declares Python 3.11, which is the existing minimum; it does not
change the runtime interpreter or the application's Python declaration.
Worker tests use synthetic responses, including loopback HTTP servers.
The library test job explicitly installs Node, Bubblewrap, UnRAR, Poppler,
Tesseract with English/French data, and the test fonts. Missing Node fails the
actual JavaScript tests. Missing native executables fail native reader tests;
they are not optional acceptance cases.
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

## Library image and native confinement

Use the same build and validation commands with `Dockerfile.library`,
`library` and distinct archive/report paths. The image retains its pinned
Python 3.14 base and runtime role. All declared archive/PDF/OCR package pins
are installed by the build. `zlib1g` includes the fixed Debian security version.
Build-only pip and uv are removed after the frozen runtime installation.
No scan exception is used.

`tests/probes/library.py` runs inside the exact image with the common Docker
restrictions. It creates its own synthetic CBZ, stored RAR4 and vector PDF in
an ephemeral tmpfs. It requires actual UnRAR, Poppler, Tesseract and eng/fra
language data. It invokes the existing reader and printed worker entrypoints,
transforms each format to CBZ, checks output page hashes and unchanged inputs,
and verifies readable OCR text. The vector PDF forces Poppler rendering.
Corrupt RAR and active PDF content must be rejected. Tool versions and results
are saved in the probe log. No captured archive or document is redistributed.

This is the existing Docker reader boundary used by the host controller with
`BOOKSYS_READER_IMAGE`. It proves a non-root worker with no effective capabilities,
no new privileges, no network route, a read-only image root and no installation
mounts or Docker socket. It does not run nested Bubblewrap or prove a deployment.

The separate host tests exercise the native alternative through
`library.sandbox.native_prefix`: an empty root, unshared namespaces, cleared
environment, one read-only synthetic input and one explicit output grant.
They verify that controller files, sibling inputs, the controller environment
and Docker socket stay absent, and that trusted code/input stay read-only.
Run `test_library_sandbox.py` with user namespaces available. A denied namespace
fails the test; do not disable seccomp, AppArmor or application isolation to make
it pass. Native host proof records the host tools/environment separately from
the image proof. The GitHub runner's native confinement remains subject to
observed CI execution. These checks change neither the host controller nor any
running image reference or reader retention policy.
