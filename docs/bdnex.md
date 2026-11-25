# BDneX identification

The metadata worker can query a private BDneX service before public discovery.
Set `BOOKSYS_BDNEX_URL` and `BOOKSYS_BDNEX_TOKEN_FILE` in the metadata role.
Leave the URL unset to keep the existing providers only.
The token file must be readable by the worker and service users.

The worker sends up to three logical names, ComicInfo hints and normalized
identity hints bound to the file. The service returns candidates at 70 or
above. It also returns candidates at 60 or above when the volume and series
words agree. A unique strong candidate at 90 or above can select the reference.
Below 90, the service can select a candidate when the series and volume point
to one reference in the full sitemap. A dated series variant needs title
agreement. Competing references and conflicting file evidence go to review.

The worker evaluates all returned candidates with `candidate-concordance-r3-v1`.
This includes service replies marked `insufficient` or `conflicting_file_evidence`.
Asset-scoped labels and embedded ComicInfo supply the evidence. Parsed filename,
OCR and captured fields cannot veto an R3 pick. Never select by score.

`web` identifies the file's own canonical Bedetheque album when the numbers
agree and its words occur in the file evidence. `cov` requires all informative
series and title words in asset labels. `title_number` requires a number-first
label and an informative title of at least two words. These modes identify
one work directly. A contradictory issue number blocks. An informative label
title after its issue marker blocks a candidate title with no shared word.
The file's own valid `Web` can explain a noisy title. Edition variants with the
same number and title can collapse to one work.
Common fields survive; edition differences stay unknown.

If R3 finds no candidate, finds several works, or has an incomplete candidate
set, the worker applies the existing exact concordance rules. Exact evidence can
identify one work in these cases. The result records the exact proof and the R3
receipt.

`cov:cover` and `series_number:cover` require cover confirmation. When R3
finds no candidate, `catalogue_prefix:cover` can route a proposal if exactly
one family whose normalized name equals or starts with the label series at a
word boundary carries that number. `(Série N)`, `Série N` and `- Série N -`
narrow the lookup to the catalogue ordinal `1re serie` or `Ne serie`. An
unmarked family can represent the first series.

A generic candidate title or an unsupported series year supplies
no direct title exemption. Periodical and dated-series rules stay unchanged.
A recorded score below 10 for the same asset and album blocks every R3 mode.
A missing or failed cover check leaves the proposal in review.
Classification and admission require the completed identity decision.

ComicInfo `Web` accepts only canonical
`https://www.bedetheque.com/BD-...-<id>.html` URLs. Other hosts, query strings,
fragments and malformed IDs are rejected. The decision receipt records `method`,
`mode`, `needs_cover`, `matched_words` and the album id in `candidate_concordance`.
For an R3 selection, the finalizer copies the receipt to `work_result.evidence`.
For a direct R3 exemption, it records the mode as the exemption basis.
Exact concordance keeps its existing title, `Web`, creator and cover checks.

The service also retrieves exact series/number proposals from the full sitemap.
The normalized file series must equal the slug series or its whole-word prefix.
The remaining slug words are decorations. Several distinct series mean review.
This retrieval uses no archive or image access. These proposals reach the same
finalizer. A missing slug title supplies no title agreement; it needs another
exemption or a passing cover. A different informative title keeps its veto.
The existing discovery path handles empty results, insufficient evidence and service outages.
Candidate-only responses from older service images also reach R3.
If the original lookup cannot identify a directly downloaded Bookys album,
Booksys can try its captured heading as one extra logical name. The heading
must supply a series and issue. Only headings bound to the exact asset supply
R3 label words. Bundle-parent headings remain discovery hints.
Already identified assets keep their work result and evidence in automatic cycles.
Metadata scheduling excludes them, even after a method or evidence revision change.
Already queued metadata tasks return the accepted result before any provider call.
Explicit reference requests retain their enrichment path. The cover guard applies
to new automatic acceptances. It does not start a bulk replay of accepted work.

Cover confirmation uses `bdnex-9de29a73-sift-height600-ratio06-v1`. A score of
40 or more passes. A score from 20 up to 40 passes only if the selected album
is the sole work with the label number across all matched families in the
full index. Editions with the same number and title count as one work.
The comparison preserves the upstream 600-pixel height and ratio
0.6. The proposed 1024-pixel long-edge variant changed the retained-lot yield
from 20/31 to 12/31, so it is not used. Covers never choose another reference.
The worker stores the applied threshold in `work_result.evidence.cover_confirmation`.
Catalogue receipts also store the lookup method, version, index, work count
and matched families. A pass via the prefix route or reduced threshold sets
`work_result.identification_method` to `catalogue-unique-cover-v1` for
targeted requeue. Metadata method v9 progressively reopens unresolved tasks.
The reader child validates the first manifest member and both archive/member
hashes. PDFs without an exemption remain in review until calibrated.
The child has resource limits. It has no new filesystem or network sandbox.
Before rollback to a worker without this guard, pause metadata acceptance.
Keep it paused until a guarded worker is restored. Deployment tooling is outside
this local change. Private input retention and exact-image release checks remain
activation prerequisites.

## Private service

Build upstream BDneX 0.10.0 at commit
`9de29a73859a527a0c8ab4b0173d2f1e0e1e6f33` with its original Dockerfile.
Build `Dockerfile.bdnex` with `BDNEX_IMAGE` set to that qualified base image.
The service runs on port 8766 as UID/GID 1000. Use a read-only root, writable
`/data`, and a read-only token file selected by `BOOKSYS_BDNEX_TOKEN_FILE`.
It needs no archive, cover or Booksys database mount. Restrict access to the
metadata worker. Permit outbound Bedetheque access for the daily index refresh.

Initialize the index with `python -m bdnex_service refresh`, or copy a qualified
upstream sitemap cache to `/data/index.sqlite`. Preserve the source cache.
The default command serves the private API and refreshes the index every 24 hours.
Refresh is sequential and publishes a validated replacement atomically.
Failed refresh or reload retains the last valid warm index.
Do not add a second refresh scheduler.

`GET /healthz` returns 200 after index loading.
`POST /v1/match` requires Bearer authentication. The client binds the response
to the asset, input digest and pinned engine. Requests are limited to 16 KiB;
responses to 64 KiB. The timeout is five seconds, with a 60-second local
cooldown after failure. A request can carry optional identity hints. The service
can return a decision tied to a reference in its candidate list. Redirects and ambient HTTP proxies are disabled.

`POST /v1/family` uses the same authentication and size limits. Send schema
`1`, the label series and the issue number. The read-only lookup returns the
album IDs, series, numbers and titles from the full warm index. It binds the
response to the request digest and engine. Older service images keep the
standard threshold and earlier R3 rules. Rebuild the BDneX image from the public
release commit to enable the lookup.

Rollback: unset `BOOKSYS_BDNEX_URL` and restart the metadata role. Existing
providers continue. Retain the service index and original archives.
The service is separate because upstream BDneX requires Python below 3.14;
the Booksys worker uses Python 3.14 and does not install BDneX.

## Verification

Run `test_bdnex_provider.py` in the locked worker development environment.
Run `test_bdnex_engine.py` in the pinned upstream image with network disabled.
The retained 100-file diagnostic qualification found seven additional identities
among 80 difficult files under the unchanged policy. All seven agreed with the
independent reference observations. This sample does not estimate library yield.
The supplied-hints adapter matched native candidate IDs and scores for all 100 inputs.

## Filename qualification

Recognition removes the known zz/Zz source prefix from logical labels. Explicit
issue markers and repeated issue captions retain the series name and its
qualifiers. Conflicting issue numbers and mixed volume/issue shapes stay
unqualified. Original archive names and bytes remain unchanged.

The earlier filename-only qualification retained 112 prior identities in a
516-file replay. That result predates the cover/Web finalizer. It does not prove
retention under the current policy. Frozen replays must count missing page and
cover proof as review and report every lost prior identity.
Metadata method v8 uses the existing progressive replay. The automatic cycle
reopens unresolved tasks on the method change. It preserves their scheduled due
time and leaves accepted identities unchanged. No bulk reset or schema conversion
is required.

A standalone title may contain one informative word when its file name and
direct captured title agree. Bundle-parent labels cannot supply this proof.
