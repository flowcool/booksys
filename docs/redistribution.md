# Distribution source and license checklist

This checklist applies to released binaries. It does not claim that a prepared
image or source configuration has already been published.

The Byparr base is pinned by digest in the Dockerfiles. Its version label refers
to upstream commit `b041dd2998110fc61789ae65e2506bd5b79edba5`. Verify that relationship
against the actual image before release. The [GPL text](../third_party/Byparr-GPL-3.0.txt)
section 1 defines Corresponding Source. Sections 5 and 6 cover modified sources
and binary distribution. Section 6(d) permits network source access with clear
instructions beside the binary. The distributor must keep that source available.

The Dockerfiles include original-code and third-party notices under
`/usr/share/doc/booksys/`. Distribution packages keep their own copyright files.
The source inventory must still be qualified against each exact built artifact.

Before publishing each image set:

1. Read the exact image SBOM and installed package/source versions. Keep all
   upstream copyright, license and attribution files. Preserve notices for
   Camoufox/browser components, fonts and native libraries as well as Python packages.
2. Retain an archive of the exact Byparr revision, its dependency lock and build
   scripts. Retain the Booksys Dockerfiles, adapter, patches and build instructions
   for the exact release revision. Mark local changes in the source record.
3. For every bundled copyleft binary outside a valid System Library exclusion,
   retain the matching upstream source and distribution patches/build scripts.
   Use the Debian/Ubuntu source package version recorded by `dpkg-query`, not the
   binary version alone. Fetch `.dsc` plus every referenced source/patch archive
   from the distribution archive or snapshot. Verify their checksums. A package
   name, moving upstream URL or hash without downloadable source is insufficient.
4. Keep a manifest mapping each image digest and installed binary/source package
   to the retained source files and notices. Review exclusions individually.
   Missing source or an unresolved redistribution condition blocks that binary's
   release. It does not require changing the license of original MIT code.
5. Make the verified source archives and manifest downloadable without credentials
   next to the image release instructions. Link them from the release and package
   description. Keep source available as long as required by the applicable license.
   Verify anonymous source downloads together with anonymous image pulls.

The offline release qualification owns artifact assembly and the final source
review. Actual source-asset availability is part of public release verification.
The source inventory is independent from vulnerability gates. Never replace a
license review with a scanner's inferred SPDX string.

UnRAR `1:7.1.8-1` has its own [full Debian notice](../third_party/unrar-copyright.txt).
The freeware terms allow bundling, but prohibit recreating RAR compression.
Keep the file's other copyright/license sections as well. Do not substitute the
commercial WinRAR trial EULA for the UnRAR source/utility license.
