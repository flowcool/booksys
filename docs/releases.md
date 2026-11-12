# Image releases and recovery

The release workflow publishes the worker, library and Byparr images as one
qualified set. It does not deploy an application. The workflow runs only for
`release: published`. Branch, PR, schedule and manual validation workflows cannot
publish. Public publication needs the maintainer's separate operation approval.

## Release qualification

Use an exact numeric `vX.Y.Z` tag. Protect tags against replacement and deletion.
The tag must resolve to the checked-out commit and an ancestor of protected
`main`. The workflow rechecks the release ID, published time, draft/prerelease
state, tag and source before publication. The first release is `v0.1.0` with the
GitHub prerelease flag. Its image tag is `0.1.0`. It cannot update `latest`.

Future stable releases update `latest` only when GitHub designates that release
as its latest stable release. Existing aliases must have an older numeric version
label. An older stable release can publish its explicit version without moving
`latest`. The complete immutable set is verified before any alias moves.

The read-only image jobs build once with the pinned BuildKit and SBOM generator.
They export an OCI archive with SPDX SBOM and maximum SLSA v1 provenance. They run
the normal synthetic probes and full vulnerability/secret scans on that artifact.
No registry credentials reach these jobs. Release builds do not consume PR build
caches. Uploaded artifacts keep the exact archive, scan identities, component
inventory, full installed notice files and logs.

The aggregate qualification checks the complete OCI graph, source/revision/version
labels, attestation-to-runtime links and scanner config identities. It requires
the reviewed corresponding-source lock described below. A fixable HIGH/CRITICAL
finding, secret, missing attestation or changed archive blocks publication.

Only the serialized publication job has package write and OIDC rights. It uses
`GITHUB_TOKEN`, never a registry PAT. Source assets need narrowly scoped contents
write rights. Skopeo copies the tested OCI archive with `--all --preserve-digests`.
It never rebuilds or converts through Docker archives. A complete OCI round trip
checks every layer, config and attestation after upload. Cosign signs each index
digest and verifies the exact identity:

```text
issuer: https://token.actions.githubusercontent.com
identity: https://github.com/flowcool/booksys/.github/workflows/release.yml@refs/tags/vX.Y.Z
```

The source artifacts upload before the images. The release API must read back
their exact SHA256 digests. The workflow refuses existing version tags and source
assets. Registry authentication failures, transport errors and ambiguous reads
stop the operation. Only explicit manifest/name-unknown registry responses mean
absence. Actual GHCR permissions, signature issuance, package visibility and
anonymous pulls/downloads require public-operation proof.

## Corresponding-source lock

The workflow deliberately refuses a release without `release/sources.lock.json`.
`release/sources.example.json` is an incomplete format example. It cannot qualify
a release. Do not rename the example and treat it as a source audit.

Generate inventories from each exact final image with `ops/release_inventory.py`.
Retain its full output under each lock image's `inventory`. For every inventory
ID and each non-package binary, add exactly one `components` entry:

```json
{"id":"dpkg:example","license":"GPL-3.0-or-later","sources":["example-1.2-source"]}
```

An individually justified exclusion instead has `exclusion`, `license` and
`rationale` fields. No inferred SPDX finding or blanket System Library exclusion
replaces review. `reviewed_non_package_components` lists additional reviewed IDs,
such as the browser build, standalone Python distribution, uv/uvx and bundled
native libraries in wheels. Every ID must have component coverage. Image-owned
filesystem paths identify installed binaries; they are not operator paths.

Each `sources` record has an immutable HTTPS URL and independently verified
SHA256. Debian/Ubuntu records point to the exact source version's `.dsc` file.
They also have `package` and `version` fields matching its Source/Version fields.
The assembler downloads that control file and every referenced source/patch
archive and verifies SHA256 and sizes. For example:

```bash
python3 ops/release_sources.py fetch-dsc HTTPS_DSC_URL VERIFIED_SHA256 SOURCE_DIRECTORY
python3 ops/release_sources.py assemble release.json artifacts sources release/sources.lock.json
python3 ops/release_sources.py prepare release.json artifacts sources > plan.json
```

The bundle also contains the exact Booksys Git revision, Dockerfiles, lockfiles,
adapters and build instructions, image notice archives and an image-to-source
manifest. Keep upstream license files intact. Review the browser source against
its installed build metadata, Python against its actual distribution, and native
wheel libraries against exact vendored versions. Version labels or upstream URLs
alone cannot prove that source correspondence. See [redistribution](redistribution.md).

Publish `corresponding-source.tar.gz` and `manifest.json` beside the binaries.
Keep them available for the applicable license period. Package descriptions and
release notes must point directly to these source assets. Actual anonymous source
and image downloads are part of release acceptance, after package visibility is
explicitly configured. New personal packages may initially be private.

For the initial prerelease, create the three immutable packages, verify their
digests and signatures, then set each package to Public in its GitHub Package
settings. GitHub's supported visibility control uses that web interface. This
expected private intermediate state is separate from a failed registry write.
Do not substitute authentication success for a public pull.

After that change, dispatch `release-verification.yml` from protected main with
the exact source revision and the `sha256:` digests of `manifest.json` and
`corresponding-source.tar.gz` from the completed publication plan. The read-only
workflow streams and hashes the public sources without credentials. Its three
ephemeral jobs use an empty registry auth file, verify the original release's
keyless identity, pull each exact graph, check SBOM/provenance and run the existing
synthetic image probes. The existing validation helper also scans each downloaded
artifact. This check binds the public retrieval to the qualified image. The jobs
retain reports only, without downloading image archives to the operator's host.
This verifier does not change the source revision or publish any artifact.

## Failed or uncertain publication

Preserve the workflow artifacts and `release-journal.json` before any recovery.
The journal records the intended operation before each write. An error after that
point means uncertain outcome, even if the client reports failure. It records the
original error and never automatically resends that write. Keep the exact original
archives and source bundle; do not rebuild as a recovery method.

1. Freeze new releases. Download the qualified artifacts and journal into a fresh
   directory. Revalidate the release and source tag. Relocate the saved plan with
   `python3 ops/release_sources.py relocate DIRECTORY release.json > plan.json`.
2. Run `python3 ops/release_images.py reconcile plan.json > reconciliation.json`.
   This is read-only. It reads all version digests, current aliases, source-asset
   digests and keyless signatures. Stop on uncertain reads or conflicting digests.
3. Review exact matches, absences, pending journal operation and alias changes.
   Record the recovery operation's commands, targets, approval and readback.
   Never delete or replace an uncertain version or source asset.
4. An explicitly approved `publish plan.json NEW_JOURNAL --resume` can retain
   matching versions and source assets and fill absent versions. It checks source
   assets by exact digest, rechecks whole graphs and verifies all signatures.
   To retain the original release workflow identity, set repository variables
   `RELEASE_RECOVERY_RUN_ID` and `RELEASE_RECOVERY_REVISION` to the approved original
   run ID and full source SHA. Then rerun only its failed publication job. It
   downloads the original qualified artifacts and enables recovery only on a
   later attempt of that exact run and SHA. Clear both variables after recovery.
   Do not rerun successful qualification/build jobs. A new local keyless identity
   cannot satisfy the release workflow identity.
5. For a partial stable promotion, all three immutable versions must still verify.
   Reconcile aliases individually and resume only from these verified digests.
   Stable alias writes are not atomic across packages. Preserve the previous
   alias digests in the journal. Stop on any new uncertainty.

A later release rollback selects a retained known-good digest. It does not
restore application data. Keep images, source bundles and proof. Reverting local
workflow changes does not retract already public source or images.

## Mechanism references

- [Pinned Skopeo copy semantics](https://github.com/podman-container-tools/skopeo/blob/v1.18.0/docs/skopeo-copy.1.md)
- [Docker build attestations](https://docs.docker.com/build/metadata/attestations/)
- [Pinned BuildKit SLSA options](https://github.com/moby/buildkit/blob/v0.34.2/docs/attestations/slsa-provenance.md)
- [Cosign container signing](https://docs.sigstore.dev/cosign/signing/signing_with_containers/)
- [GitHub package access and visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
