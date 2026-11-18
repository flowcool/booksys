# BDneX candidate provider

The metadata worker can query a private BDneX service before public discovery.
Set `BOOKSYS_BDNEX_URL` and `BOOKSYS_BDNEX_TOKEN_FILE` in the metadata role.
Leave the URL unset to keep the existing providers only.
The token file must be readable by the worker and service users.

The worker sends at most three logical names and existing ComicInfo hints.
It sends no archives or absolute storage paths. BDneX proposes candidates
with scores of at least 70. Booksys keeps its existing identity and admission
checks. A score does not establish identity. Unknown fields stay unknown.
Concordant references use existing exact-page enrichment. Uncertain results
and service outages fall back to the existing public providers.
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
cooldown after failure. Redirects and ambient HTTP proxies are disabled.

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
