# Bedetheque parser component qualification

Owner: `private-work-item`. Accepted scope: spec 0003. This is a T1 component proof,
not an activated metadata pipeline or a 145-file identification result.

## Selected component and reuse

Pin cbzforge `678233154bf40eab50d932b8d0d57f9093aa5017`, 8 November 2026.
The MIT source and notice are retained in `library/vendor/cbzforge/`.
Source: [BedethequeScraper.php](https://github.com/chevalierbenoit/cbzforge/blob/678233154bf40eab50d932b8d0d57f9093aa5017/src/Scraper/BedethequeScraper.php).

cbzforge is PHP/Symfony. It is not an importable Python package. The small
`library.french_metadata` adapter ports its album-list/page selectors and name
normalization into existing BeautifulSoup. No new dependency or catalogue app is
installed. The port preserves all writers and other supported contributor roles.
It fixes the observed article-order difference between `Les geeks` in h1 and
`Geeks (Les)` in microdata, with an isolated regression fixture.

## Discovery and access proof

Current robots contains repeated wildcard groups. Combine their rules: `/ajax/`
and cover paths are disallowed. The upstream `/ajax/series` discovery is therefore
excluded. No solver, account or provider challenge bypass is introduced.

Two public search-form URLs return HTTP 200 but only the generic search form.
They are unexpected responses, not evidence that a title is absent. Do not retry
these routes as empty search results. The stale Spawn URL copied from upstream's
unit test returns HTTP 301. It remains unavailable; no redirect was followed.

The public alphabetical series index is linked by the site itself. Retained G,
L and N pages are below the 2 MiB response cap. Exact series label matching uses
normalized accents and leading/trailing articles; all exact references survive.
Otherwise every token-concordant series is retained. More than three plausible
series returns a truncated ambiguity, not the first three as a decision.
The Geeks, Largo Winch and Naruto pages retain 14, 61 and 77 album rows respectively.
The full retained cache and request ledger remain under ignored qualification tmp.

`QualificationSource` enforces cumulative <=50 requests, sequential reads and
>=1 s spacing (2 s default). This proof used 17 requests including robots and the
negative route probes. Replay uses the retained cache with live access disabled.
No cover, hoster, download queue, archive or live catalogue is accessed by this tool.
The source freezes HTTP results for this proof; production TTL/retry orchestration
is a later T3 task.

## Neutral interface

- `parse_series_search` returns all reference links; it makes no match decision.
- `parse_series_albums` returns all visible canonical album rows.
- `parse_album_page` parses the URL's canonical page metadata.
- `parse_album_editions` retains all visible compact edition cards as separate
  candidates. Largo Winch 212 retains 26 notices with 20 date and 11 ISBN values.
- `ReferenceCandidate.to_match_candidate()` maps to flat source/upstream/url,
  title/series/number and supported fields, plus field provenance. `work_ref` stays
  unknown. `edition_ref` is the source record ID, not edition confirmation.
- `discover` returns status/candidates/reason/truncated. Missing data, no candidate,
  unexpected HTML, provider unavailable, robots denial and budget exhaustion remain
  distinct. An incomplete response is never an automatic work decision.

Language, family, genres and adult assertions require actual source metadata.
`Franco-Belge`, `Manga` and `Comics` are explicit source family labels. Genres stay
multiple tags. An explicit erotic genre marks adult true; missing recommended age
stays unknown. Adult does not become an invented age 18. Integral status comes
from an explicit headline/badge. A number is not invented for an unnumbered album.

Secondary values can differ across edition cards. The adapter retains them.
The work evaluation layer must compute common supported values and field-level
conflicts. It must not select the canonical page's publisher/ISBN for the file.

## Measured scope and limits

The versioned case manifest has eleven cases. Retained public references cover
BD, manga, a standalone, an integral, multiple editions, bundle member labels and
an explicitly erotic source genre. Seven positive reference concordances match
explicit supplied labels. Four cases abstain: wrong issue/title combination,
several works without a disambiguating title/issue, an absent label, and the stale
upstream URL. No wrong labelled match occurs in this small sample. Exact edition
confirmation is zero.

The codec/cover component proof is a separate T1 artifact. T1 is not globally
complete until those proofs and independent review are integrated. Unknown family,
missing fields and unsupported layouts must remain visible. No Komga is installed.

## Reproduce

Use the existing frozen environment without sync/install. Run
`python -m unittest discover -s tests -p test_french_metadata.py -v` and typecheck
`library/french_metadata.py library/french_source.py ops/qualify_french_metadata.py`.

Use `ops/qualify_french_metadata.py --cases
 tests/fixtures/french-metadata/qualification-cases.json --output <owned-proof-dir>
 --input-pack <frozen-metadata-inputs-v2.json>` with the retained cache copied into
that owned proof directory. Without `--live`, a cache miss returns unavailable.
Add `--live` only for an authorized bounded requalification. The script reports
exact inputs and separate work/abstention/field/exact-edition metrics.

Keep original files untouched. This component does not transform, move or write
managed library copies. Runtime wiring, file/source binding, retry lifecycle,
classification paths and activation remain T2–T9 integration work.
