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

Booksys records the selected identity and uses the exact page for enrichment.
A missing or invalid page leaves the selected reference in place. The existing
classification and admission checks still apply. The existing discovery path
handles empty results, insufficient evidence and service outages. Candidate-only
responses from older service images retain their existing path.
If the original lookup cannot identify a directly downloaded Bookys album,
Booksys can try its captured heading as one extra logical name. The heading
must supply a series and issue. Candidates from that search require agreement
with evidence from the file before they can enter identity validation.
Already identified assets do not trigger new BDneX requests. Existing metadata
scheduling supplies progressive replay. No bulk replay is started by index refresh.

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

The retained 516-file replay increases identity concordance from 112 to 153,
with all 112 prior identities retained. This measures offline concordance
against retained references, not independently verified accuracy for every gain.
The metadata method change uses existing progressive replay; no bulk reset.

A standalone title may contain one informative word when its file name and
direct captured title agree. Bundle-parent labels cannot supply this proof.
