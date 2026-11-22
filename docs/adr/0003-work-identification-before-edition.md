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

Permit one bounded reference-cover fetch and one isolated visual comparison for an otherwise eligible series-and-issue identification without a concordant asset-bound title or primary creator. This is a confirmation step after candidate selection. It is not cover ranking or discovery. Failure or insufficient similarity leaves the identity in review. Keep unknown edition fields, original bytes and existing conflict rules. Keep the private BDneX service free of archive access. Store the receipt in the existing asset evidence payload. No database schema conversion is intended.
