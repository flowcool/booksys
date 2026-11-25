# ADR 0004 — Group webtoon chapters by Telegram index title

Accepted on 25 November 2026.

Use the retained `WEBTOONS_LISTS` index title to group documents from
WEBTOONSVF. Keep the display title and the document provenance. Get the chapter
number from the original filename. Use review when the evidence is incomplete
or conflicts. Keep `webtoon` copies under `webtoon/<series>/<chapter>/` and
exclude this source path from the work identification yield.

The retained comparison found 0 matches in 50 Bedetheque checks on the sampled
translated Telegram files. ComicVine returned 0 plausible matches for the 30
largest WEBTOONSVF series. These samples support a separate reference path.

Use the public MangaUpdates API once per normalized index title. Require a unique
exact Manhwa title or associated title in the returned results before automatic
acceptance. Record the reference and operator decisions in Booksys state. Add
accepted series metadata to each chapter's ComicInfo. Use the index title for
the collection path even after a reference decision. Preserve the original PDF.

[Collection rule](../specs/0003-work-identification-and-metadata.md)
[Operator commands](../operations.md#look-up-and-review-webtoon-series)
