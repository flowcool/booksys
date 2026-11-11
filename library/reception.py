"""Tracked reception paths; never scan unrelated downloader files."""
from pathlib import Path
from library.primitives import LibraryError

def reception_path(root: Path, folder: str, name: str) -> Path:
    relative = Path(folder) / name
    if Path(folder).is_absolute() or Path(name).name != name or '..' in relative.parts:
        raise LibraryError('Unsafe pyLoad reception path')
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.resolve().is_relative_to(root.resolve()):
        raise LibraryError('Reception escape or symlink')
    return path
