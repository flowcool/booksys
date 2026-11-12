"""Preserve source metadata as safe, offline documents beside downloaded files."""

from __future__ import annotations

import html
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from booksys_core.errors import BrowserError, CollectionError
from booksys_core.scheduler import Deferred

ImageFetcher = Callable[[str, str], tuple[bytes, str]]


class CoverError(CollectionError):
    """The text is preserved, but its local cover still needs a retry."""


class CoverBrowserError(CoverError, BrowserError):
    """Text was saved, but cover identity refresh failed in the browser."""


def atomic_bytes(path: Path, content: bytes) -> None:
    # Random temporary files cannot follow a pre-existing <name>.part symlink.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".booksys-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def text(value: Any) -> str:
    # Source prose and filenames are data, never raw HTML or Markdown markup.
    return re.sub(r"([\\`*_{}\[\]()#!|~])", r"\\\1", html.escape(str(value), quote=False))


def write_fiche(source: str, record: dict[str, Any], package: dict[str, Any], root: Path,
                fetch_image: ImageFetcher | None, *, known_cover: str | None = None) -> str:
    folder = Path(package["folder"])
    if folder.is_absolute() or len(folder.parts) != 1 or folder.name in {"", ".", ".."}:
        raise CollectionError("Unsafe pyLoad package folder")
    destination = root / folder
    if destination.is_symlink() or not destination.resolve().is_relative_to(root.resolve()):
        raise CollectionError("Package folder escapes download root")
    destination.mkdir(parents=True, exist_ok=True)
    fiche = destination / "fiche.md"
    marker = f"<!-- Booksys fiche v1: {source} -->"
    owned = fiche.is_file() and not fiche.is_symlink() and fiche.read_text().startswith(marker + "\n")
    if fiche.is_symlink() or (fiche.exists() and not owned):
        raise CollectionError("Refusing to overwrite an unmanaged fiche.md")
    covers = [destination / ("couverture" + ext) for ext in [".jpg", ".png", ".webp", ".gif"]]
    if any(p.is_symlink() or (p.exists() and not owned and str(p.relative_to(root)) != known_cover) for p in covers):
        raise CollectionError("Refusing to overwrite an unmanaged cover")
    cover = next((p for p in covers if p.is_file() and p.stat().st_size), None)
    page = record["page"]

    def render() -> None:
        rows = [marker, f"# {text(record['title'])}", "", f"Source : {source}",
                f"Collecte : {page['captured_at']}", f"Paquet pyLoad : {package['pid']}", ""]
        if cover:
            rows += [f"![Couverture]({cover.name})", ""]
        elif page.get("cover_url"):
            rows += ["Couverture locale : récupération en attente.", ""]
        rows += [f"- **{text(k)}** : {text(v)}" for k, v in page["details"].items()]
        rows += ["", "## Description", "", text(page["description"]), "", "## Formats annoncés", ""]
        rows += ["- " + text(v["metadata"]) for v in record["downloads"].values()]
        rows += ["", "## Fichiers associés", ""]
        rows += [f"- {text(f['name'])} — identifiant pyLoad {f['fid']}" for f in package["links"]]
        rows += ["", "Document généré par Booksys. Conserver les notes personnelles dans un autre fichier.", ""]
        atomic_bytes(fiche, "\n".join(rows).encode())

    # Preserve readable text even when cover transfer fails.
    render()
    if page.get("cover_url") and cover is None:
        if fetch_image is None:
            raise CoverError("Cover retrieval not configured")
        try:
            data, extension = fetch_image(page["cover_url"], source)
        except Deferred:
            raise
        except BrowserError as exc:
            raise CoverBrowserError(str(exc)) from exc
        except (OSError, ValueError, CollectionError) as exc:
            raise CoverError(str(exc)) from exc
        if extension not in {".jpg", ".png", ".webp", ".gif"} or not data:
            raise CoverError("Unsupported cover format")
        cover = destination / ("couverture" + extension)
        atomic_bytes(cover, data)
        render()
    return str(folder)
