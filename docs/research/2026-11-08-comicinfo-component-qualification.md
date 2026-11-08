# ComicInfo and cover-component qualification

Owner: private-work-item, T1. Accepted scope: spec 0003 and ADR 0003.
This is component qualification. It does not activate identification or change
library admission. Komga compatibility is not claimed.

## Selected component

Reuse ComicTagger `ImageHasher` at commit
`fa11a3456cbe5aa9f54f47bdc374ba26b2b946f3` (2026-09-06), source file
`comictaggerlib/imagehasher.py`. Its byte-identical copy, source SHA-256,
Apache-2.0 license and attribution are in `library/_vendor/`.
The upstream repository has no separate NOTICE file at this revision.
The narrow Booksys wrapper accepts an already decoded validated image, computes
the upstream 64-bit perceptual hash and returns candidate hash distances.
It retains ties. Blank and tiny covers abstain. A distance is not a confidence
percentage. This component never decides identity, edition, adult content or age.
The caller must keep image decoding inside the existing bounded worker.

No new runtime dependency is needed. The module uses the standard library and
Pillow 13.0.0, already pinned in this project. Do not install the whole desktop or
CLI application for this function. PyPI reports ComicTagger 1.5.5, which is a
different older package than the inspected 2026 source. Our pin is the explicit
source commit and file hash, not a false claim to run that published CLI version.
The source pin avoids adding the full CLI dependency set, including requests,
RapidFuzz, pycountry, filename libraries and ICU.

Sources: [upstream module](https://github.com/comictagger/comictagger/blob/fa11a3456cbe5aa9f54f47bdc374ba26b2b946f3/comictaggerlib/imagehasher.py),
[license](https://github.com/comictagger/comictagger/blob/fa11a3456cbe5aa9f54f47bdc374ba26b2b946f3/LICENSE),
[PyPI release metadata](https://pypi.org/pypi/comictagger/1.5.5/json).

## Codec and archive decision

Keep the existing Booksys ComicInfo 2.0 validator and qualified archive transport.
Do not add ComicTagger's archive stack or comicinfoxml for equivalent behavior.
The upstream core ComicRack codec does not serialize GTIN/ISBN. The optional
MIT `comicinfoxml` 0.5.1 codec supports GTIN and Tags, but these elements do not
validate against this project's pinned ComicInfo 2.0 XSD. Changing the codec alone
would create an incompatible schema pair. ISBN stays in the central record.
The published comicinfoxml 0.5.1 wheel SHA-256 is
`758f10a96fa62ac933ae100d5d4b97df6f3b682a6fc93b9c95531908363494f9`.
Its module is byte-identical to the inspected source pin; GTIN/Tags mappings were
verified from that downloaded wheel without installing or executing it.
Unknown XML extension fields must remain retained source evidence, outside the
2.0 export, rather than silently becoming authoritative fields.

Sources: [ComicTagger core codec](https://github.com/comictagger/comictagger/blob/fa11a3456cbe5aa9f54f47bdc374ba26b2b946f3/comicapi/tags/comicrack.py),
[comicinfoxml source](https://github.com/comictagger/comicinfoxml/blob/034b5d39c2973fac4ebb42f61ebe09ac66385df5/comicinfoxml.py),
[comicinfoxml published pin](https://pypi.org/pypi/comicinfoxml/0.5.1/json),
[local XSD](../../library/schemas/ComicInfo-2.0.xsd).

## Integration contract for T2–T6

| Central field | ComicInfo 2.0 field / rule |
| --- | --- |
| title, series, number | Title, Series, Number; omit unknown series/number |
| contributors | Writer, Penciller, Inker, Colorist, Letterer, CoverArtist, Editor by supported role |
| summary | Summary; preserve explicit operator correction |
| publisher, dates, language | Publisher, Year/Month/Day, LanguageISO only when supported |
| genres | Genre as multiple comma-separated tags; keep source assertions centrally |
| adult | Central sourced boolean; retain explicit adult/erotic genre, no invented numeric age |
| recommended_age | Keep unknown absent; export only a separately supported ComicInfo age label |
| ISBN | Central record; no GTIN/ISBN element in the chosen 2.0 export |
| reference URL | Web; provenance/source ID/date remain in central assertions and fiche |
| operator notes and bookmarks | Preserve Notes and supported Pages/Page annotations across enrichment |
| unknown fields/conflicts | Retain raw evidence and review state; omit unsupported/conflicted authoritative values |

The existing writer is **not yet qualified for the accepted work policy**. The
executed probe confirms that it currently maps adult to `Adults Only 18+`, and
rebuilds Notes without merging source annotations. T2–T6 must correct those two
behaviors and extend field mapping before activation. Upstream codecs also remove
supported XML fields when the metadata object omits them, so changing codec does
not replace a merge policy. XML 2.0 has an ordered sequence; merged fields must
be reordered before validation. Preserve manual overrides before export.

## Offline qualification

Use the versioned synthetic probes to qualify the exact candidate.

## Limits and next action

Synthetic covers prove determinism, distances, tie retention and blank abstention.
They do not establish a production score threshold or real-corpus coverage. T4/T8
must measure positive covers, unrelated covers and visually identical reprints on
the frozen representative corpus. Cover evidence can support an album proposal;
it cannot establish a precise edition or recommended age.

Select the pinned cover component. Retain the existing XML/archive machinery.
Complete merge/mapping and real-corpus evidence in the integration work. Do not
activate the old adult mapping or claim unknown-field preservation by the current
writer. Future Komga import needs a separate compatibility proof.


Focused validation: two cover tests pass. Strict mypy passes for the wrapper and
qualifier. Three existing CBZ/XML transform tests pass. The existing CBR conversion
test cannot run in this isolated native environment: no working UnRAR executable
is available (`RarCannotExec`). This does not qualify CBR conversion and is not
reported as a passing check. No archive transport code changed here. The parent
runs integrated checks with its qualified reader image and toolchain.
