# Work identification, metadata and classification before Komga

Status: accepted by the operator after the eleven-question grilling on 8 November 2026.
Owner: `private-work-item`.
This is the current policy for this change. It supersedes earlier requirements
that make an exact edition a prerequisite for identification or library admission.
Earlier integrity, provenance and recovery requirements still apply.

## Outcome

Produce usable, classified CBZ files with sourced metadata. Identify the album
before trying to establish its exact edition. Reuse existing software components
before writing another metadata or matching engine. Komga deployment comes later.
The running implementation is not evidence that this accepted change is delivered.

## Accepted decisions

1. A reliable title identifies an album. Use series and issue number when they
   exist. A standalone album needs no invented series or issue number. Exact
   publisher, date, ISBN and edition remain unknown until supported.
2. Several edition records may describe one album. Retain their common supported
   information and continue. Keep edition-specific differences unknown. Do not
   combine conflicting records into an invented edition.
3. Classify automatically when file evidence agrees with a reference record.
   Keep ambiguous cases for review. Retain candidates and the reason for doubt.
   A first search result or highest score alone is not a sufficient decision.
4. Start with existing metadata, filename and captured source information.
   Reuse valid prior inspection and metadata evidence. A second upstream source is not mandatory for
   work-level identification; an unresolved contradiction still prevents it.
5. Organize classified copies by BD / Manga / Comics, then series and issue.
   Use the title when a series or issue does not exist. Keep genres as multiple
   metadata tags. A genre does not create another physical copy. Keep an unknown
   family explicit rather than inventing one to complete a directory path.
6. Mark adult content when a source explicitly identifies adult or erotic content.
   Keep the source reference. A missing recommended age stays unknown. Do not
   infer a numeric age from an adult tag, a cover, or missing source data.
7. An identified album can be classified when sources disagree on a secondary
   field. Mark that field for review. Keep its assertions; omit an unresolved
   value from authoritative exports. Identity contradictions still require review.
8. One integral in one file remains one book. Do not split its pages into albums.
   A download bundle containing complete separate files is handled album by album.
9. Identical bytes share one managed copy and retain every source origin.
   Different files remain distinct versions until comparison. Do not assert that
   they are the same edition, or silently replace one version with another.
10. Convert technically valid unidentified comics into CBZ in the holding area
    labelled “À identifier”. Keep them available outside the classified collection.
    Keep identification and classification status separate from technical validity.
11. Retry unresolved identification weekly. Retry sooner after a method improvement
    or useful new evidence. Reuse inspection and extraction results when their
    inputs and method remain valid. A temporary provider error uses bounded retry;
    it is not a contradiction, a missing record, or a permanent negative match.

## Metadata and provenance

Use Bedetheque as the primary discovery reference for French comics. Qualified
providers may supply additional assertions. Two tools reading one site are one
upstream source. Store reference IDs, URLs, retrieval dates and field provenance.
Source prose remains untrusted data.

Keep title, series, issue, integral status, contributors, summary, language,
publisher, dates, ISBN, genres and adult/age fields when supported. Unknown values
stay empty. Preserve explicit operator corrections across later enrichment.
A secondary-field conflict does not erase other supported fields.

Keep a readable fiche and the central machine-readable reference. Write supported
ComicInfo fields into each CBZ. Preserve ISBN centrally even if a chosen codec
cannot represent it. Verify the codec/schema pair before adding GTIN or changing
ComicInfo version. Komga's future import behaviour must be tested separately.

## Reuse boundaries

Qualify the reusable Bedetheque parser from MIT-licensed cbzforge and the archive,
matching or ComicInfo components from Apache-licensed ComicTagger. Preserve their
notices and pin the source/version. Reuse functions rather than adopting a whole
catalogue application. A component is selected only after a representative proof.

Keep Booksys-specific code limited to source/file binding, orchestration, field
mapping, classification policy and review state. Do not inherit destructive
conversion or arbitrary first-match behaviour. Code from a repository with an
unclear licence is not copied into this project without a clear reuse grant.

## Outputs and lifecycle

Keep original downloads unchanged. All managed comic copies are CBZ. Validate
source integrity and converted page content independently. Keep source and output
hashes separate. Preserve journals, decided evidence and recovery revisions.

Classified paths reflect the accepted family/series/issue organization. Sanitize
paths and use stable IDs to avoid collisions. Unknown identity uses the separate
holding area. Missing classification stays explicit and remains reviewable.
Moving a case from the holding area into the classified collection is tracked and
recoverable. Metadata changes do not authorize an unsafe overwrite or purge.

Use the single existing pyLoad. Restrict processing to tracked Booksys inputs;
shared Lunii downloads are outside this comic pipeline. Keep the existing disk
reserve and private credentials. No additional account, paid service, external
backup or production preparation is required for this lab.

## Acceptance

- Known albums, standalone works, several editions, integrals, bundles, manga,
  comics and an explicitly sourced adult genre have representative fixtures.
- Automatic matches, wrong matches, abstentions and missing fields are measured
  separately on a frozen real corpus. Compare with the retained 145-file,
  one-identity baseline. Report coverage honestly; do not count unknown editions
  as exact-edition confirmations.
- Empty search results, contradictory identity evidence, secondary-field conflict,
  unavailable providers and exhausted budgets have distinct statuses.
- Ambiguous candidates never become an arbitrary first match. Common album fields
  survive edition ambiguity. Unknown values and manual corrections are retained.
- Classified and unidentified CBZ copies pass independent page/hash and ComicInfo
  readback. Originals stay unchanged. Exact duplicates and different versions are
  handled without a silent collision or replacement.
- Weekly and evidence-triggered retries reuse unchanged expensive results and do
  not cause a busy loop.
- Work-level identification needs no invented edition data.
  The monitoring page exposes actual stage and reason.
- Run relevant seam tests and type checks during work. Run one full integrated
  suite for the final candidate, complete independent review, then commit.
- Record the exact lab candidate, operation plan, source preservation, readback and
  recovery before activation. Keep Komga installation outside this delivery.

## References

- [Decision](../adr/0003-work-identification-before-edition.md)
- Tasks
- [Reuse research](../research/2026-11-08-french-comic-metadata-reuse.md)
- [Earlier library specification](0001-portable-comic-library.md)
