# ADR 0003: Identify works and reuse metadata components

Status: updated for the catalogue-unique cover decision of 25 November 2026.

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

Use candidate-centric concordance (`candidate-concordance-r3-v1`). Asset labels
must contain the candidate's informative series and title words with the same
number. A number-first informative title or the file's own Bedetheque `Web` can
also prove one work. Parsed filename, OCR and captured fields support candidates;
they do not veto an R3 pick. If R3 abstains, apply exact concordance with its
existing vetoes and keep both results. Never rank by score. Collapse edition
variants without inventing edition metadata.

Series and number alone, generic titles and an unsupported series year require
cover confirmation. The standard threshold is 40. Use 20 when the proposed
album is the sole work with that number in the full BDneX index across the
normalized label series and its whole-word extensions. Collapse editions with
the same number and title. R3 can route a prefix match to cover if exactly one
matched family has that number. Map `Série N` label markers to the catalogue
ordinal and allow an unmarked first series.

A recorded score below 10 for the same asset and album blocks identification.
Keep number and disjoint label title rejections. Keep periodical and dated-series
rules. Preserve accepted work in later cycles. Store the mode and matched words
in the existing result payload. Record the catalogue method and version, work
count, matched families and threshold. Mark new-path identifications
`catalogue-unique-cover-v1` for targeted requeue.

Use the versioned read-only BDneX family lookup. An older service keeps the
standard threshold and earlier R3 rules. Metadata method v9 progressively
reopens unresolved tasks. Rebuild the BDneX service image from the public
release commit.
