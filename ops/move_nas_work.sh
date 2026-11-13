#!/usr/bin/env bash
set -euo pipefail
for name in bundle-inputs collector-covers web-covers attachments pieces tmp; do
    source="${BOOKSYS_NAS_FILES_ROOT:?}/work/$name"
    destination="${BOOKSYS_LOCAL_ROOT:?}/$name"
    mkdir -p "$destination"
    rsync -a "$source/" "$destination/"
    printf '%s source=%s destination=%s\n' "$name" "$(find "$source" -type f -printf . | wc -c)" "$(find "$destination" -type f -printf . | wc -c)"
done
