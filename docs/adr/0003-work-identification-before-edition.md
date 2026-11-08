# ADR 0003: Identify works before exact editions and reuse metadata components

Status: accepted by the operator after grilling, 8 November 2026.
Owner: `private-work-item`.
Supersedes ADR 0002 only where exact edition proof blocks work identification,
classification or admission of an otherwise valid CBZ. Integrity and recovery stay.

## Context

The exact-edition path produced one accepted identity on a frozen corpus of 145
valid files. Missing French discovery coverage, lost provider fields and strict
edition prerequisites limited the result. Archive validation and successful unit
tests did not establish useful identification coverage.

The operator needs identified albums, enriched metadata and classified files for
later Komga use. Exact edition data is useful when known. It is not required to
recognize an album. The operator also asked us to reuse existing GitHub software.

## Decision

Separate work identification, exact edition confirmation, technical validity and
classification. An identified work can enter the classified collection with an
unknown edition. A valid unidentified file can enter the separate holding area.
Keep the stricter edition-confirmation path for evidence that actually supports it.

Use metadata and filenames first. Spend cover/OCR work on unresolved cases.
Keep uncertainty at field level where possible. Keep ambiguous work identity for
review. Retry unresolved cases on the accepted schedule and on changed evidence.
The complete behavioural rules are in [spec 0003](../specs/0003-work-identification-and-metadata.md).

Qualify reusable Bedetheque parsing from cbzforge and suitable ComicTagger
components before writing equivalent functions. Preserve source licences, pin
versions and test our required fields. Keep a small integration layer for source
binding, policy and review. A full desktop or catalogue server is not required.

## Alternatives considered

- Keep exact-edition proof as the admission gate: strong edition semantics, but it
  blocks useful albums when the scan has no ISBN or edition data.
- Select the first or highest-scored record: higher apparent throughput, but it
  silently assigns wrong albums or editions. Rejected.
- Adopt a complete catalogue application now: existing UI and storage features,
  but it does not establish French source coverage and changes the current scope.
- Build another metadata engine: full control, with duplicated maintenance of
  parsers, matching and archive metadata that existing components already cover.

## Consequences

Coverage and exact-edition confirmation have separate metrics. A rise in usable
classified copies must not be reported as a rise in proven exact editions.
Unknowns and conflicting assertions remain visible. An integral stays one book;
separate complete files in a download bundle retain their own identities.

Provider coverage and file evidence still limit automatic identification. No
coverage percentage is promised before the representative corpus is measured.
Cover similarity proposes a match; it does not prove an exact edition or age.
Genre and adult classification require sourced assertions.

Component reuse still needs qualification. Current ComicTagger provider defaults
are not a French Bedetheque solution. Its core ComicRack writer does not map GTIN;
the optional codec and schema version need explicit compatibility proof. Keep
ISBN in the central reference even when an archive format cannot carry it.

## Delivery and recovery

The accepted design is not deployed by this document. Existing isolated commits
are inputs for review, not a released candidate. Keep the single downloader,
original bytes, private credentials, journals, evidence and recovery copies.
Use schema-compatible recovery and retained images for the exact activated lab
candidate. Komga installation remains a later activity.
