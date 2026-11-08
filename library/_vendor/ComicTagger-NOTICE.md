# ComicTagger image hashing component

Copyright 2013 ComicTagger Authors. Licensed under Apache-2.0; see
`ComicTagger-LICENSE.txt`. The upstream copyright and licence header remains
inside the module. Upstream has no separate NOTICE file at this revision.

Source: https://github.com/comictagger/comictagger/blob/fa11a3456cbe5aa9f54f47bdc374ba26b2b946f3/comictaggerlib/imagehasher.py

Pinned commit: `fa11a3456cbe5aa9f54f47bdc374ba26b2b946f3` (2026-09-06).
This file is copied byte-for-byte. SHA-256: `c20fcfd7aaa00d41f36c310193e53e90b4ed315ee00041333297a0f682c117fd`.
This source pin is deliberate. PyPI ComicTagger 1.5.5 is a different older release;
we do not install or imply compatibility with that CLI. Only this module is reused.
It imports the Python standard library and Pillow, already pinned by Booksys.

## Filename components

The legacy `FileNameParser` class and its two placeholder constants are extracted
from `comicapi/filenameparser.py` at the same pinned commit. The Apache header is
retained. Imports are reduced to the used standard-library modules and the local
`issuestring` module. The parser class itself is unchanged. The unused new lexer
and its text2digits dependency are excluded. `comicapi/issuestring.py` is copied
byte-for-byte as `comictagger_issuestring.py`.

Booksys accepts only explicitly qualified French single-album shapes through
`library/names.py`. The upstream parser does not establish source identity.
