# Portable comic library

Status: updated for the lab runtime of 13 November 2026.
See the [architecture](../architecture.md) and the
[work identification scope](0003-work-identification-and-metadata.md).

Keep a local library usable without Bookys or a specific reading application.
Process tracked Booksys PDF, CBZ and CBR inputs. Preserve original downloads.
Keep unrelated downloader files outside this pipeline.
Publish independent CBZ copies with supported ComicInfo.xml metadata.
Keep source and output hashes, file origins and transformation journals.

Validate source integrity and converted page content independently.
Reject unsafe archive paths and incomplete inputs.
A bundle contains separate complete files. A published integral remains one work.
Preserve parent/member hashes and deterministic staging for bundle retries.

Use local PostgreSQL as the operational authority.
Use work_items for collection, intake, metadata, copies and routing.
Keep stage transitions and file operations recoverable.
Do not repeat an uncertain downloader or file operation without reconciliation.
Keep credentials and operational evidence outside Git.

Organize classified copies by family, series and issue when supported.
Keep missing metadata explicit. Preserve original files during later enrichment.
Komga deployment and synchronization remain outside this scope.
