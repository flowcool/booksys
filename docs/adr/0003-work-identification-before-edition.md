# ADR 0003: Identify works and reuse metadata components

Status: updated for the candidate concordance decision of 24 November 2026.

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
they do not veto an R3 pick. Several possible works require review. Never rank
by score. Collapse edition variants without inventing edition metadata.

Series and number alone, generic titles and an unsupported series year require
the existing cover confirmation at threshold 40. A recorded score below 10 for
the same asset and album blocks identification. Keep number and disjoint label
title rejections. Keep periodical and dated-series rules. Preserve accepted work
in later cycles. Store the mode and matched words in the existing result payload.
No schema conversion or BDneX service change is required.
