# Public image examples

The intended packages are `ghcr.io/flowcool/booksys-worker`,
`ghcr.io/flowcool/booksys-library` and `ghcr.io/flowcool/booksys-byparr`.
They target `linux/amd64`. Configuration alone does not prove package availability.
Check the release and anonymous pull results before use.

The first planned release is GitHub prerelease `v0.1.0`. Its numeric image tag is
`0.1.0`. It has no `latest` tag. Use a verified digest for repeatable operation.
See [the generic Compose example](../compose.ghcr.example.yaml). It prints CLI help
unless the operator supplies installation-specific configuration. It does not
create a downloader, replace a host service or activate a library cycle.
Local Dockerfile builds remain available.

After publication, inspect the release's digest list, SBOM, provenance and signature
verification instructions. Pull each exact digest anonymously before adoption.
Retain the old digest and state recovery files before a runtime change.
The registry provides reader binaries. The library controller still runs on the
host and owns scheduling, mounts, state and recovery. Publishing these images does
not establish reader retention or runtime readiness for an installation.

Image licenses differ from the Booksys MIT source license. Read the
[third-party notices](../THIRD_PARTY_NOTICES.md) before redistribution.

## Release verification

See [releases and recovery](releases.md) for exact-artifact publication,
corresponding-source assets, signature identity and recovery after uncertain writes.
