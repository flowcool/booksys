# Reader copy selection

Booksys compares the SHA-256 of every page in reading order. It uses the
validated CBZ output manifest. This includes converted PDF pages. It keeps
the first verified classified copy that matches the confirmed work and pages.
A proven edition stays accessible if the default lacks that edition proof.
Uncertain comparisons appear in the publication reason. Originals and their
origins stay in the catalogue.

An asset already in holding can gain a duplicate reference when identification
completes. Its holding copy stays in place. Periodicals and webtoons use their
existing publication rules.

## Preview

Run the report with the selected catalogue and reader library. This command
uses read-only catalogue access and verifies the managed files of each proposed
pair. Repeat it after the catalogue changes.

```sh
python -m library.cli --state "$STATE" reader-selection --library "$LIBRARY"
python -m library.cli --state "$STATE" reader-selection --library "$LIBRARY" \
  --select "$KEEP_ASSET" "$RETAIN_ASSET"
```

Use the full asset hashes from the report. Each selection names one copy to
keep and one to retain in recovery. The report proposes the earliest eligible
copy first. It covers classified copies with current committed journals.

## Apply and restore

Apply only an approved pair during an exclusive maintenance window. Stop the
discovery and copies workers before you apply or restore a selection. The
discovery worker holds a shared `library-cycle.lock` while it runs. Use the
qualified writer and the normal writer preflight options for this library.
In the Booksys lab, use `--lab`.

```sh
python -m library.cli --state "$STATE" reader-selection --library "$LIBRARY" \
  --reception "$RECEPTION" --lab --select "$KEEP_ASSET" "$RETAIN_ASSET" --apply
python -m library.cli --state "$STATE" reader-selection --library "$LIBRARY" \
  --reception "$RECEPTION" --lab --restore "$JOURNAL_ID" --apply
```

The selection saves `selection.yaml` in
`$LIBRARY/.recovery/reader-selection/$JOURNAL_ID/` before it moves the full
managed copy. It checks both file hashes after the move and reads back the
catalogue. A copy that already serves as the default for another asset is
protected. Keep the journal and recovery directory. To resume an interrupted
operation, run the same command again. Stop if the journal, catalogue or
file hashes disagree. Restore an applied selection with this version before
rolling back the worker if the old exposures are required.

At the end of the maintenance window, restart discovery first, then copies.
