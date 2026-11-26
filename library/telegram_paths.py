"""Lexical rules shared by Telegram registration and source readers."""
from pathlib import Path
import re

from booksys_core.errors import LibraryError

ARCHIVE_ROOT = Path("/data/booksys/telegram-archive")
ARCHIVE_PREFIX = "telegram-archive/"
SCOPES: dict[str, tuple[int, int | None]] = {
    "BD_X18": (1235693416, None),
    "bd_fr": (1219883119, 96714),
    "webtoons-vf": (1288796629, None),
}
FORMATS = frozenset((".cbz", ".cbr", ".pdf"))


def telegram_filename(value: object) -> str:
    if (not isinstance(value, str) or not value or value.startswith((".", "ug-tmp"))
            or re.search(r"[/\\\x00-\x1f\x7f-\x9f\ud800-\udfff]", value)
            or len(value.encode("utf-8")) > 255):
        raise LibraryError("Unsafe Telegram filename")
    if Path(value).suffix.lower() not in FORMATS:
        raise LibraryError("Unsupported Telegram format")
    if Path(value).stem.lower().endswith((".part", ".partial", ".tmp", ".crdownload", ".download")):
        raise LibraryError("Partial Telegram filename")
    return value


def archive_relative(path: str, channel: int, topic: int | None, message: int, original_name: str) -> str:
    """Validate the stored archive form and return the path within the archive mount."""
    prefix, sep, relative = path.partition("/")
    scope, _, filename = relative.partition("/")
    if prefix != ARCHIVE_PREFIX[:-1] or not sep or SCOPES.get(scope) != (channel, topic):
        raise LibraryError("Telegram archive path is outside the receipt scope")
    filename = telegram_filename(filename)
    stem = f"{channel}_{message}_" if scope == "webtoons-vf" else f"{message}_"
    if not filename.startswith(stem) or len(filename) <= len(stem) + len(Path(filename).suffix):
        raise LibraryError("Telegram archive filename does not match the message")
    suffix = Path(filename).suffix.lower()
    if suffix != Path(original_name).suffix.lower() or (scope == "webtoons-vf" and suffix != ".pdf"):
        raise LibraryError("Telegram path and receipt formats differ")
    return relative
