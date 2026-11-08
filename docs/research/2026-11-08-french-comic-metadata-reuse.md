# French comic metadata tooling reuse

Owner: `private-work-item`. Operator research-first direction: 8 November 2026.

## Goal and boundary

Identify French comics, enrich their metadata and classify usable files before
Komga. Komga deployment, reader selection and catalogue replacement are outside
this research stage. Prefer existing tools over a new matching/tagging engine.

Repositories were searched by name/description, then README references. The second
search found more relevant French projects. Each selected repository was cloned
once. Findings below come from implementation inspection, not feature lists alone.
No candidate tool was installed or executed. No live archive was changed.

## Findings

| Candidate | Useful existing functions | Limits for this pipeline |
| --- | --- | --- |
| Bullarr | Standalone French metadata workflow, Bedetheque, series matching, preview/review without volume writes, genres, summary, issue number and BD/comics/manga sorting | Ambiguous albums can fall back to the first match. ISBN is parsed but omitted from ComicInfo output. Age is preserved rather than established. No explicit repository licence found. |
| cbzforge | MIT; Bedetheque search, album confirmation, ComicInfo, archive conversion and renaming before Komga | Assisted workflow. Series search starts with the first result. Bedetheque adapter does not fill genre/age. Conversion can delete its input; use owned copies only. |
| ComicTagger | Mature standalone Python CLI; JSON/dry-run, metadata YAML, archive tagging, cover-based candidate matching and renaming; Apache-2.0 | Built-in provider is ComicVine. No existing Bedetheque talker found in the searches. Built-in provider mapping supplies neither ISBN nor genres/maturity. Cover similarity is a candidate score, not edition proof. |
| Komf | Metadata provider framework and local ComicInfo writer; MIT | No Bedetheque provider found. Complete enrichment/write workflows use Komga/Kavita library entities. Search-only REST may work without a media server, but this was not started or qualified. |
| BDnex | Existing French Bedetheque/bdfugue discovery, sitemap title matching and cover comparison; ComicInfo | Older code, interactive/destructive paths and broad sitemap strategy. Root LICENSE says CC0; package metadata says MIT. Do not describe it as unambiguously MIT. |
| vsoeiro/bedetheque | Recent Python parser for series/album metadata: ISBN, publisher, issue/date, edition information, contributors and covers | Fetches known series references; no verified bounded automatic title search in the repository. Database-oriented toolkit. No explicit licence found. |
| ComicRack CE + Bedetheque Scraper v2 | Maintained ComicInfo ecosystem and French reference-search workflow | Windows desktop/plugins. Not a Linux batch metadata service. Useful as a format/client/reference, not the proposed runtime engine. |
| ComicRack AmazonScrapper | Amazon/Comixology metadata, manual correction workflow | Windows plugin and human selection for each book. Not a mass unattended workflow. |
| ComiXed | Web catalogue, scanning, pagination, archive metadata/organisation | Different scope. No PDF support or French Bedetheque provider found. Do not install it to solve identification. |

## Rejected as an unattended engine

- BDtagM builds title search parameters but does not send them. Its best-match
  fallback takes the first result after failed cover matching. Its native-CBZ path
  can report tagging success without writing ComicInfo. README scope exceeds the
  inspected implementation. MIT does not establish functional suitability.
- aubustou/bedetheque-scraper is a real narrow CLI, but excludes ISBN from XML,
  keeps every cover-ranked candidate, selects the first file sharing an issue
  number and mutates/moves archives directly. No explicit licence found.

## Output compatibility

ComicTagger's built-in ComicRack tagger does not map GTIN. The optional
`comicinfoxml`/`cix` codec reads/writes GTIN, including published version 0.5.1.
GTIN is in the ComicInfo 2.1 draft, not the 2.0 schema used by this project.
Komga's exact GTIN import behaviour remains unqualified. Preserve ISBN in the
central source assertions regardless of the chosen archive codec. Do not equate
a writer's supported fields with fields actually supplied by its provider.

Existing XML handling can preserve unknown root elements but rebuilds `Pages`.
A source inspection cannot establish image preservation: compare every page hash
before and after any candidate write. Keep explicit provenance and unknown fields.
Do not infer an adult classification from missing age data or a generic filename.
Use a sourced genre/tag when available; retain unknown otherwise.

## Recommendation and next proof

Bullarr is the closest functional match for French identification/enrichment and
sorting. Qualify its preview/review workflow on owned copies before adopting its
automatic write/selection path. Record the missing licence; do not copy its code
into Booksys without a clear reuse grant. cbzforge is a useful assisted fallback.
ComicTagger remains a credible reusable tag/matching engine if a narrow French
connector is actually needed. Do not build that connector before testing the
existing French workflows. Komf is secondary while Komga is not deployed.

Use a fixed representative set containing ordinary albums, standalone works,
several editions of one issue, integrals, manga and at least one known adult genre.
Record candidate reference, exact edition evidence, title/series/issue/ISBN,
contributors, summary, sourced genres, unknown age and proposed destination.
Measure correct matches, wrong matches, abstentions and missing fields separately.
A tool must refuse an ambiguous match. Inspect preview first; write only owned
copies, then independently validate ComicInfo and page hashes. Do not promise a
publication or identification rate before that proof.

## Pinned primary sources

- [lbesnard/bdnex](https://github.com/lbesnard/bdnex/tree/31f135652e62332c24b88cb86f89f76e6feb212d) — `31f135652e`.
- [vsoeiro/bedetheque](https://github.com/vsoeiro/bedetheque/tree/e2d7933485dea91d07135eeb8dc24ac4bb26b99d) — `e2d7933485`.
- [comixed/comixed](https://github.com/comixed/comixed/tree/dd2fef987b395609c214ec340c2c4e8c526077ad) — `dd2fef987b`.
- [maforget/ComicRack_AmazonScrapper](https://github.com/maforget/ComicRack_AmazonScrapper/tree/71587193aa16e7979c641f44183d09d20d170ed4) — `71587193aa`.
- [Snd-R/komf](https://github.com/Snd-R/komf/tree/d8a34e9df29ddaa6941c302df216812ee6d525e9) — `d8a34e9df2`.
- [comictagger/comictagger](https://github.com/comictagger/comictagger/tree/fa11a3456cbe5aa9f54f47bdc374ba26b2b946f3) — `fa11a3456c`.
- [chevalierbenoit/cbzforge](https://github.com/chevalierbenoit/cbzforge/tree/678233154bf40eab50d932b8d0d57f9093aa5017) — `678233154b`.
- [trocool/BDtagM](https://github.com/trocool/BDtagM/tree/1bd3d4e9188f94dd3a271ad31d980117d4590c7c) — `1bd3d4e918`.
- [aubustou/bedetheque-scraper](https://github.com/aubustou/bedetheque-scraper/tree/981330a23e80cec1822781ccabe9ca4322ab3042) — `981330a23e`.
- [gwenvador/bullarr](https://github.com/gwenvador/bullarr/tree/6e0b50547418a262c08e15b6a663cdd533554c0d) — `6e0b505474`.
- [maforget/ComicRackCE](https://github.com/maforget/ComicRackCE/tree/7a3ab2e3315f16845441558a1542a9c2d476df34) — `7a3ab2e331`.
- [maforget/Bedetheque-Scrapper-2](https://github.com/maforget/Bedetheque-Scrapper-2/tree/0cda4994b04a1e44d5bc6d4e2db1d17804b1de0e) — `0cda4994b0`.
- [comictagger/comicinfoxml](https://github.com/comictagger/comicinfoxml/tree/034b5d39c2973fac4ebb42f61ebe09ac66385df5) — `034b5d39c2`.

Public Bedetheque robots/home/operator and one sitemap were fetched read-only.
Homepage and sampled sitemap returned HTTP200; the sitemap contains 6,413
references in about1.27MB. This proves public access for that sample, not a bulk
service guarantee. Existing public search mechanics were verified in the scraper
source; no inferred private endpoint or new Cloudflare solver was introduced.

Detailed source notes and operation evidence remain under ignored
`tmp/evidence/improvement-delivery/` and `/data/booksys/multipart-qualification/`.
