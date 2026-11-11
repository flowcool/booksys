# Contribution CI and dependency maintenance

CI does not require installation credentials or access to a running Booksys instance.
All PRs, including forks and Markdown changes, start validation and security workflows.
There is no workflow-level path filter. GitHub activation must be verified after bootstrap.

## Required results

Require `required`, `secrets`, `dependency-review` and `codeql` after verifying their
actual check names on the first PR. `required` aggregates the validation jobs. It
fails for failures or cancellation. Docs-only PRs skip image builds and the full
suite, but run workflow/security policy checks and generic Compose validation.
A failed scope calculation selects all slices. A failed scope job fails the aggregate.
Weekly and manual validation run all slices and publish nothing.

`ops/ci_scope.py` compares complete local Git ranges. PRs use the merge base and
both PR SHAs, not a paginated file API. Renames include old and new paths. Shared
modules, CI files, locks and unknown paths select every slice. Library modules
select both application and reader images. Byparr-only files select its image.
Any selected slice runs the complete offline
suite once. Each selected image uses the common exact-artifact probe/scan workflow.
The suite installs native archive/PDF/OCR tools and Node explicitly. The existing
core and library type checks run on Python 3.14, the current runtime interpreter.
The declared source compatibility remains >=3.11. The suite uses a pinned,
temporary PostgreSQL 18.6 service and a private synthetic DSN file. It cannot
select an operator database. No local stack is needed for these CI prerequisites.

Gitleaks scans every tracked current file through `git archive`, including Markdown,
and the complete reachable history, including merge-result diffs. This catches unchanged content moved into a new
path and secrets removed in a later commit. Scheduled scans repeat the history gate.
There are no blanket docs/test exclusions. A false positive requires an exact reviewed
fingerprint in `.gitleaksignore`, with its rationale in the PR. Never ignore an entire
rule, test directory or document tree. Final history rewriting changes fingerprints;
review and regenerate only those exact fingerprints before the first publication.
Privacy review separately covers names, addresses, paths and provider activation notes.

## Privileges and review

Default tokens are read-only. Checkouts do not persist credentials. Jobs use hosted
runners and bounded timeouts. PRs cannot publish images, log in to a registry, access
provider secrets or reach an installation. No `pull_request_target` workflow executes
PR code. Dependency review has only contents read and does not write PR comments.
CodeQL analyzes Python without a build. Its job alone requests `security-events: write`
for SARIF. GitHub permits CodeQL analysis of fork PRs with the restricted fork token;
this does not grant repository write or secrets to their code. JavaScript embedded in
Python strings is outside the standalone JavaScript extractor's source inventory.

This publication scope uses no external automated reviewer. The maintainer reviews
the patch and validation proof. Branch rules require PRs, current green checks,
an up-to-date base and resolved threads. No second human approval is required.
There is no review-agent workflow, provider secret or review-app grant.

## Update ownership

Renovate owns ordinary PEP 621/uv, Actions, Dockerfile and Compose updates. It runs
Monday 00:00-03:59 Europe/Zurich with at most three concurrent branches/PRs. Merges
remain manual. Major upgrades need dependency-dashboard approval before a PR.
Python's declared >=3.11 compatibility stays unchanged. The library base stays on
3.14. Pin updates within that line remain visible. Dependency lock maintenance uses
uv and refreshes `uv.lock`. Dockerfile-only `beautifulsoup4`, `PyYAML` and build-only
`uv` pins use one narrow custom regex manager. Self-package examples are excluded.

Release age is three days when a timestamp exists. `timestamp-optional` permits
GHCR digest updates without timestamps. It does not auto-merge them. Native Debian
pins cannot be resolved from an unspecified apt repository: Renovate extraction can
report `unknown-registry`. Review those pins manually against the distribution and
retain weekly image vulnerability reports. Fixed installer/scanner versions and
checksums in shell scripts also need manual upstream review. Do not silently claim
updater coverage for them. The exact-image severe-fixable gate remains mandatory.

Dependabot owns security repair for supported ecosystems. The current GitHub matrix
explicitly supports uv v0.11 version and security updates, and dependabot-core has
uv lockfile security-fix resolution. `open-pull-requests-limit: 0` disables its ordinary
PRs, while security PRs use their separate limit. Renovate vulnerability alerts and
OSV repair are disabled to prevent duplicate repair PRs. Enable dependency graph,
alerts and security updates in repository settings. Verify actual uv graph/advisory
and repair behavior after activation; local YAML does not prove that service behavior.

## Authoritative mechanisms

- [GitHub Actions security](https://docs.github.com/en/actions/reference/security/secure-use)
- [CodeQL fork PR permissions](https://docs.github.com/en/code-security/code-scanning/troubleshooting-code-scanning/resource-not-accessible)
- [Dependabot ecosystem support](https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories)
- [Dependabot PR limits](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#open-pull-requests-limit-)
- [Renovate PEP 621 and uv](https://docs.renovatebot.com/modules/manager/pep621/)
- [Renovate missing timestamps](https://docs.renovatebot.com/configuration-options/#minimumreleaseagebehaviour)
