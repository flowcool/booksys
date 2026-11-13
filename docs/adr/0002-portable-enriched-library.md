# Portable enriched library

Status: updated for the lab runtime of 13 November 2026.
See the [architecture](../architecture.md) for the current implementation.

Keep a local catalogue independent of the final reading application.
The existing URL-keyed Bookys catalogue remains a collection export.
Keep original downloads unchanged. Publish independent CBZ copies of validated
Booksys PDF, CBZ and CBR inputs. Write supported metadata to ComicInfo.xml.
Keep source and output hashes separate. Retain provenance and recovery journals.
Copies do not replace a backup.

Preserve PDF page geometry and visible content during conversion.
Use qualified Poppler rendering when direct image extraction is not equivalent.
Keep original PDF text, vectors and links in the original file.

Treat a download bundle as a source of separate complete files.
Retain the original container, member names and parent/member hashes.
Use deterministic staging and verified manifests for retries.
The parent title cannot establish the identity of its members.

The final reading application and its deployment remain outside this scope.
