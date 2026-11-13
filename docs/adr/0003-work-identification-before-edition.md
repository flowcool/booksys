# ADR 0003: Identify works and reuse metadata components

Status: updated for the lab runtime of 13 November 2026.

Identify the album from filenames, captured source information and metadata.
Keep unknown values explicit. Retain provenance for supported fields.
Use the [work identification specification](../specs/0003-work-identification-and-metadata.md).

Reuse the qualified Bedetheque parser from cbzforge and ComicTagger components.
Preserve source licences and notices. Pin component versions.
Keep the integration responsible for source binding and metadata mapping.
A complete desktop or catalogue server is outside this scope.

Retain original bytes, private credentials, journals and recovery copies.
Use schema-compatible recovery for the exact candidate.
Komga deployment remains a later step.
