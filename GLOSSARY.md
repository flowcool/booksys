# Booksys library

Terms for captured sources, bibliographic identity and retained files.

## Language

**Bookys**:
The external source website.

**Booksys**:
This local collection and library project.

**Source record**:
A captured Bookys page and its outgoing links. One source record can describe several works.
_Avoid_: Work, edition, downloaded file

**Work**:
One album or volume, independent of its editions and digital files. A published integral is a separate work.
_Avoid_: pyLoad package, series

**Series**:
A named group of related works. A series is not a downloaded batch.

**Edition**:
A published version of a work, distinguished by supported bibliographic evidence such as language, publisher or revised content.
_Avoid_: File format, scan

**Variant**:
A distinct digital representation of the same edition, such as another scan or file format.
Use this term when the common edition is established.
_Avoid_: Edition, identical mirror

**Asset**:
A file identified by its exact bytes. Several origins can provide the same asset.
_Avoid_: Hoster link, work

**Reception**:
The original bytes received by the shared downloader, including Booksys and Lunii files.
Generated fiches and covers stored beside those files are not reception assets.

**Library**:
Managed, validated CBZ copies prepared for a future reader. A copy can have an identified work and an unknown edition, or remain unidentified.
_Avoid_: Reception, pyLoad queue

**Promotion**:
Admission of a validated asset as the selected variant of a confirmed edition.

**Assertion**:
A metadata value with its source and retrieval date. An assertion can disagree with another source.

**Override**:
An explicit operator decision that takes priority over subsequent source assertions until the operator changes it.

**Library artifact**:
The generated file admitted to the library. Its bytes can differ from the source asset.
_Avoid_: Original download, source asset

**Page extraction**:
Retention of an embedded image that represents one complete visible PDF page without an unresolved appearance change.

**Page rendering**:
A raster image of a visible PDF page. The copy retains its appearance but does not retain searchable text, vector structure or links.
_Avoid_: Lossless extraction

**Download bundle**:
A received container holding separate complete comic files. A bundle is not an album, edition or selected variant.
_Avoid_: Published integral, multipart representation

**Bundle member**:
One complete file retained from a download bundle. Each member has its own asset identity and keeps its relationship to the bundle and source record.
_Avoid_: Automatically identified edition

**Work identification**:
Recognition of an album by a reliable title, with series and issue when they exist.
It does not assert an exact edition.
_Avoid_: Edition confirmation, filename guess

**Edition confirmation**:
Recognition of the exact published version using evidence that supports its distinguishing details.
_Avoid_: Work identification, highest-ranked candidate

**File version**:
A distinct file representing a work. Its edition can remain unknown.
_Avoid_: Exact duplicate, proven same-edition variant

**Classification**:
Organization of an identified work by supported family, series and issue, with genres as tags.
_Avoid_: Exact-edition confirmation, one physical copy per genre

**Library admission**:
Retention of a validated CBZ in the managed collection, including an unidentified holding area.
_Avoid_: Exact-edition confirmation

**Unidentified copy**:
A technically valid managed comic whose work identity is not established.
_Avoid_: Corrupt file, missing download

**Acquisition**:
The tracked receipt of required source files, including original and fallback
packages and their retained origins. An acquisition can contain several assets.
_Avoid_: Work, single pyLoad package

**Package retirement**:
Removal of a completed tracked acquisition's pyLoad records while retaining its
downloaded files, bindings and processing history.
_Avoid_: Source deletion, acquisition abandonment

**Activity observation**:
A dated report about a Booksys stage or element. It describes known work and
does not authorize or schedule that work.
_Avoid_: Processing queue, submission journal
