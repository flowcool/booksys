# Retire completed Booksys packages from pyLoad

Accepted by the operator on 8 November 2026 after a design interview.
Delivery owner: Bead `private-work-item`. Query Beads for execution state.
This document defines the target. It does not describe an active feature.
Implementation and runtime acceptance are tracked in the owning Bead.
Use the retirement runbook for controls and recovery.

## Outcome and scope

Keep the shared pyLoad instance free of completed Booksys packages.
Keep downloaded source files and their full local history.
Apply the same checks to existing and future Booksys packages.
Do not change Lunii packages or other unowned packages.

This extends the [AllDebrid-first amendment](0001-portable-comic-library.md#accepted-alldebrid-first-download-amendment--8-november-2026).
Parking remains valid while acquisition or technical validation is incomplete.
After the checks below pass, local history replaces the requirement to retain
the package and its IDs inside pyLoad. Historical IDs remain in Booksys.
Captured URLs remain available for a later explicit acquisition.
Retirement must not trigger automatic resubmission.

## Confirmed decisions

1. Retire an eligible package at the end of a successful processing cycle.
   Do not add a fixed retention delay.
2. Require technical validation of every required input. Do not wait for
   bibliographic identity, cover generation or publication.
3. Keep failed packages until a mirror succeeds or the operator explicitly
   abandons the acquisition. Failure, age and retry count do not imply abandonment.
   This feature does not add an automatic abandonment rule.
4. Preserve original downloads, derived bundle inputs, recovery copies,
   manifests and evidence. Retirement removes pyLoad records only.
5. Keep downstream processing available after removal and after restart.
6. A retired package cannot use the current native parking rollback.
   Disabling retirement stops future removals. It does not restore deleted IDs.

## Eligibility

Bind each candidate to a tracked Booksys submission. Verify the exact package
identity, file IDs and captured URL set. A name prefix alone is insufficient.
Recheck ownership and processing state immediately before the removal request.

Account for the complete required hoster set. Validate each required file or
prove that an identical validated copy remains available. A finished transfer
alone is insufficient. A title, filename or published sibling is not proof.

For a bundle, retain the unchanged parent and verified member manifest.
Complete technical processing of every required child before retirement.
Identity may remain pending. An unprocessed or rejected required child blocks
success retirement. Do not treat a partial extraction as a complete bundle.

For multipart inputs, require the complete tracked part set, assembly proof
and validated outputs from the integrated multipart implementation.
Keep part paths, hashes and group provenance. A single finished part is not
an eligible acquisition. Recheck this contract after the concurrent yield work.

Treat original and fallback packages as one acquisition for success decisions.
A failed original may retire after a complete fallback succeeds and its source
history is retained. Preserve failed bytes and fallback directories.
Unused mirror links may be cancelled after complete success proof.
Do not require their redundant downloads to finish.

Require no active transfer, extraction or other pyLoad processing on the target.
Use targeted stopping where needed. Verify completion before removal.
Do not pause, restart or clear the shared downloader globally.
Unknown state, changed files or missing proof blocks that candidate.

## Durable local history

Use keyed, schema-versioned YAML records. Extend existing canonical records
where practical. Do not create another authority for live transfer state.
The terminal record describes the completed acquisition and its retirement.
It must not present a retained snapshot as current pyLoad state.

Before removal, save the submission binding, original PID/FIDs, complete URLs,
package name, original and fallback relationships, source paths, sizes, hashes,
bundle or multipart manifests, technical outcome and proof references.
Record intent before the API write. Record confirmed absence after readback.
Make recovery possible if the process stops between these steps.

An absent package with matching retirement intent requires reconciliation.
An absent package without such evidence remains an unexpected disappearance.
After an uncertain API result, inspect the exact target before any further write.
Do not repeat the removal blindly or recreate a package to repair its absence.

## Whole-chain contract

| Component | Required behavior after retirement |
| --- | --- |
| Collector | Retain submission history. Do not submit the same source again. |
| Fiches and central export | Keep paths and file references. Resume pending text or cover work from local history. |
| Intake | Keep validated origins valid. Distinguish retirement from `package_missing`. Continue local integrity checks. |
| Bundles and multipart sets | Keep all source and member bindings usable without pyLoad. |
| Mirror routing | Recognize completed acquisition before querying a removed package. Do not wake spare mirrors. |
| Identity and operator resolution | Continue local work. Retirement alone must not invalidate a prepared confirmation. Real input changes still invalidate it. |
| Conversion and publication | Resolve and verify retained inputs. Preserve pending identities and existing copies. |
| Operator page | Show retired acquisition history and current library state without false missing-package errors. |
| Restart and recovery | Reconcile removal intent and confirmed absence. Resume without duplicates or repeated removals. |

Coordinate retirement with collector, intake and routing state updates.
The cycle lock alone does not establish exclusion with the separate collector.
Define lock order and recheck state before committing each transition.
Bound work per cycle and retain fair progress for intake and other jobs.

## Technical gates before activation

Inspect the exact installed pyLoad API and active addons again for the candidate.
The inspected core `delete_packages` path removes database records and sends
`package_deleted` events. It also releases plugins. Core behavior alone does
not prove that every installed hook preserves files.
Qualify active extraction, plugin cleanup and deletion scripts.
Keep credentials out of workers, evidence and logs.

Use native targeted APIs. Do not edit pyLoad SQL or its private database files.
Provide a preview of eligible and blocked packages with explicit reasons.
Use versioned tooling for removal. Record exact targets, commands, expected
proof, stop conditions and recovery in the owning Bead before runtime writes.
Stop dependent removals after an uncertain write or target drift.
Keep independent offline work available.

## Acceptance

1. Existing and newly received tracked packages use the same eligibility rules.
   Lunii and unowned packages remain unchanged.
2. Isolated tests cover ordinary inputs, duplicates, complete and incomplete
   bundles, multipart sets, primary success, fallback success, failed inputs,
   active processing, ownership drift and absent packages without retirement.
3. Fault tests cover interruption before removal, lost API response and
   interruption after removal but before local confirmation. Recovery neither
   resubmits the source nor repeats an uncertain write blindly.
4. After removal, a complete cycle and a process restart preserve fiche/export
   references, validated origins, bundle and multipart access, mirror state,
   pending identity work, prepared operator confirmations and publication.
   Confirmed retirement remains visible in the operator page.
5. Pending cover or identity work continues from local inputs. Retirement does
   not create false success for a missing file or an invalid child.
6. Qualification uses an isolated pyLoad instance or isolated configuration.
   Shared pyLoad is not a test fixture. A later controlled lab operation proves
   the exact integrated candidate on selected eligible Booksys packages.
7. Compare retained source and derived-input hashes before and after removal.
   Read back exact target absence and preservation of unrelated pyLoad IDs.
8. Before activation, stopping retirement and restarting ordinary processing
   must remain safe with already retired records. Record schema compatibility.

## Delivery sequence

1. Recheck the integrated yield and multipart work tracked in `private-work-item`.
2. Implement terminal history and adapt every dependent reader before deletion.
3. Implement preview, targeted removal and interrupted-operation reconciliation.
4. Verify the isolated whole-chain acceptance for the exact candidate.
5. Prepare the scoped lab operation and recovery record before runtime removal.

## Recovery limits

Stop new retirement operations on failure. Retain all history and proof.
Inspect a pending intent against the exact package before choosing recovery.
Keep downstream code able to read retired records when disabling the feature.
Do not roll back to code that requires every historical package to exist.
Do not claim that recreating a package restores its previous PID or FIDs.
Any later acquisition uses a new explicit attempt with its own provenance.
