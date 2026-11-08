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
V1 admits only a complete representation in one file; multipart representations remain pending.
_Avoid_: Edition, identical mirror

**Asset**:
A file identified by its exact bytes. Several origins can provide the same asset.
_Avoid_: Hoster link, work

**Reception**:
The original bytes received by the shared downloader, including Booksys and Lunii files.
Generated fiches and covers stored beside those files are not reception assets.

**Library**:
The selected, validated CBZ copies prepared for a future reader.
_Avoid_: Reception, pyLoad queue

**Promotion**:
Admission of a validated asset to the library as the selected variant of an edition.

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
