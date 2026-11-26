"""Read one immutable Telegram receipt page per discovery turn."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
from stat import S_ISREG
import time
from typing import Any, BinaryIO, Mapping

from booksys_core.diagnostics import emit
from booksys_core.errors import LibraryError
from library.catalog import Catalog
from library.locking import bounded_acquisition
from library.operational_state import CollectorState
from library.primitives import digest, telegram_origin_id, valid_telegram_index_title
from library.reception import reception_path
from library.telegram_intake import submit_origin
from library.telegram_paths import ARCHIVE_PREFIX, SCOPES, archive_relative, telegram_filename
from library.webtoons import series_key

FEED_ROOT = Path("/data/booksys/telegram-receipts")
MAX_ROWS = 32
MAX_ROW_BYTES = 64 * 1024
MAX_HEAD_BYTES = 4096
MAX_PAGE = 99_999_999
STREAM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
POLL_SECONDS = 30

# Canonical digests of the retained path, size and SHA-256 for the two trials.
TRIALS = {
    (1235693416, 5321): "a92b9273c533e133d55b6b05b01676dc6670d4c167d5dd74f028fad623525226",
    (1219883119, 166816): "3c7feccc393f6c3c74cfc57f756eb5e92ec5377551b520df0a644898aa578563",
}


class BadPage(LibraryError):
    """An immutable page or existing origin needs operator review."""


def open_feed(root: Path, scope: str, name: str) -> BinaryIO:
    path = reception_path(root, scope, name)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            raise LibraryError("Telegram feed input is not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def json_object(data: bytes) -> dict[str, Any]:
    value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_fields, parse_constant=invalid_constant)
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise LibraryError("Invalid Telegram feed schema")
    return value


def invalid_constant(value: str) -> None:
    raise LibraryError(f"Invalid Telegram feed JSON constant {value}")


def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(pairs) != len(result):
        raise LibraryError("Duplicate Telegram feed JSON field")
    return result


def webtoon_index(row: dict[str, Any]) -> dict[str, Any]:
    references, reposts = row.get("origins", []), row.get("reposts", [])
    if (not isinstance(references, list) or not isinstance(reposts, list)
            or any(type(message) is not int or not 0 < message < 2**31 for message in reposts)):
        raise LibraryError("Invalid Webtoon index or repost references")
    for ref in references:
        if (not isinstance(ref, dict) or type(ref.get("anchor")) is not int or not 0 < ref["anchor"] < 2**31
                or not isinstance(ref.get("index"), str)
                or not re.fullmatch(r"WEBTOONS_LISTS/[1-9][0-9]{0,9}", ref["index"])
                or not isinstance(ref.get("title"), str)):
            raise LibraryError("Invalid Webtoon index reference")
    evidence: dict[str, Any] = {"origins": references, "reposts": reposts}
    titles = {series_key(ref["title"]) for ref in references if valid_telegram_index_title(ref["title"])}
    review = row.get("index_review")
    if review not in (None, "missing-valid-index-evidence", "conflicting-index-titles"):
        raise LibraryError("Invalid Webtoon index review reason")
    if len(titles - {""}) > 1 or review == "conflicting-index-titles":
        evidence["index_review"] = "conflicting-index-titles"
    elif "index_title" in row:
        title = row["index_title"]
        if not valid_telegram_index_title(title):
            raise LibraryError("Invalid Telegram index title")
        if (not references or review is not None or titles != {series_key(title)} or "" in titles
                or any(not valid_telegram_index_title(ref["title"]) for ref in references)):
            evidence["index_review"] = "missing-valid-index-evidence"
        else:
            evidence["index_title"] = title
    elif review is not None:
        evidence["index_review"] = review
    return evidence


def receipt_origin(row: dict[str, Any], scope: str) -> tuple[str, dict[str, Any]]:
    channel, topic, message = row.get("peer"), row.get("topic"), row.get("message")
    if (type(channel) is not int or "topic" not in row or (topic is not None and type(topic) is not int)
            or (channel, topic) != SCOPES[scope] or type(message) is not int or not 0 < message < 2**31):
        raise LibraryError("Telegram receipt identity does not match the feed scope")
    raw_name = row.get("original_name")
    original_name = telegram_filename(re.sub(r"[\x80-\x9f]", "_", raw_name) if isinstance(raw_name, str) else raw_name)
    relative = row.get("path")
    if not isinstance(relative, str) or not relative.startswith(scope + "/"):
        raise LibraryError("Invalid Telegram archive-relative path")
    path = ARCHIVE_PREFIX + relative
    archive_relative(path, channel, topic, message, original_name)
    size, checksum, proof = row.get("size"), row.get("sha256"), row.get("completion_proof")
    reference = row.get("completion_ref")
    if (type(size) is not int or not 0 < size < 2**63 or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or proof not in ("native", "baseline-stable-2pass") or (scope == "webtoons-vf" and proof != "native")
            or not isinstance(reference, str)
            or not re.fullmatch(rf"[A-Za-z0-9_-]{{1,100}}/{message}\.json", reference)):
        raise LibraryError("Invalid Telegram completion proof, size or SHA-256")
    caption = row.get("caption", "")
    if not isinstance(caption, str) or re.search(r"[\x00\ud800-\udfff]", caption):
        raise LibraryError("Invalid Telegram caption")
    telegram = {"channel": channel, "topic": topic, "message": message, "original_name": original_name,
                "sha256": checksum, "caption": caption, "completion_proof": proof, "completion_ref": reference,
                "receipt_sha256": digest(row)}
    if scope == "webtoons-vf":
        telegram.update(webtoon_index(row))
    return telegram_origin_id(channel, message), {"source": "external:" + path, "path": path,
        "pid": None, "fid": None, "size": size, "sha256": None, "availability": "pending", "telegram": telegram}


class TelegramPull:
    def __init__(self, catalog: Catalog, scopes: tuple[str, ...], *, root: Path = FEED_ROOT,
                 streams: Mapping[str, str] | None = None):
        self.catalog, self.scopes, self.root = catalog, scopes, root
        self.streams = dict(streams or {})
        self.turn = 0

    @classmethod
    def configured(cls, catalog: Catalog) -> TelegramPull | None:
        if os.environ.get("BOOKSYS_TELEGRAM_PULL") != "1":
            return None
        scopes = tuple(part.strip() for part in os.environ.get("BOOKSYS_TELEGRAM_SCOPES", "").split(","))
        if len(set(scopes)) != len(scopes) or any(scope not in SCOPES for scope in scopes):
            raise LibraryError("BOOKSYS_TELEGRAM_SCOPES must list distinct supported scopes")
        streams = {}
        for entry in filter(None, os.environ.get("BOOKSYS_TELEGRAM_STREAMS", "").split(",")):
            scope, _, stream = entry.strip().partition("=")
            if scope not in scopes or scope in streams or not STREAM_ID.fullmatch(stream):
                raise LibraryError("Invalid BOOKSYS_TELEGRAM_STREAMS scope or stream ID")
            streams[scope] = stream
        return cls(catalog, scopes, streams=streams)

    def poll(self, *, now: datetime) -> None:
        scope = self.scopes[self.turn]
        self.turn = (self.turn + 1) % len(self.scopes)
        with bounded_acquisition(time.monotonic() + POLL_SECONDS - 1):
            self.poll_scope(scope, now=now)

    def poll_scope(self, scope: str, *, now: datetime) -> None:
        page = 1
        operation = "Read checkpoint"
        try:
            checkpoints = CollectorState(self.catalog.directory, database=self.catalog.backend()._database)
            saved = checkpoints.telegram_checkpoint(scope)
            page = saved.get("page", 0) + 1
            operation = "Read head"
            stream, ready = self.read_head(scope)
            selected = self.streams.get(scope)
            if (selected is not None and stream != selected
                    or saved and stream != saved["stream_id"] and stream != selected):
                self.report(scope, page, "blocked", "operator_decision_pending", "Select the replacement stream")
                return
            if not saved or stream != saved["stream_id"]:
                page = 1
                saved = {"stream_id": stream, "page": 0}
                operation = "Record stream"
                checkpoints.save_telegram_checkpoint(scope, saved)
            if "blocked" in saved:
                self.report(scope, page, "blocked", "operator_decision_pending", saved["blocked"])
                return
            if ready < page - 1:
                self.report(scope, page, "waiting", "invalid_content", "Head is behind the checkpoint")
                return
            if ready == page - 1:
                return
            try:
                operation = "Read committed page"
                entries = self.read_page(scope, page)
                operation = "Register receipt"
                for oid, origin in entries:
                    self.register(scope, page, oid, origin, now=now)
            except BadPage as exc:
                operation = "Record blocked page"
                checkpoints.save_telegram_checkpoint(scope, {**saved, "blocked": str(exc)})
                self.report(scope, page, "blocked", "operator_decision_pending", str(exc))
                return
            operation = "Save checkpoint"
            checkpoints.save_telegram_checkpoint(scope, {"stream_id": stream, "page": page})
            self.report(scope, page, "completed", "none", "Receipt page registered")
        except OSError:
            self.report(scope, page, "waiting", "transfer_wait", f"{operation} unavailable")
        except (LibraryError, ValueError, RecursionError) as exc:
            reason = "storage_busy" if str(exc) == "storage_busy" else (
                "invalid_content" if operation == "Read head" else "recovery_wait")
            self.report(scope, page, "waiting", reason, f"{operation} failed")

    def read_head(self, scope: str) -> tuple[str, int]:
        with open_feed(self.root, scope, "head.json") as stream:
            data = stream.read(MAX_HEAD_BYTES + 1)
        if len(data) > MAX_HEAD_BYTES:
            raise LibraryError("Telegram feed head exceeds its size limit")
        head = json_object(data)
        stream_id, ready = head.get("stream_id"), head.get("ready_page")
        if (not isinstance(stream_id, str) or not STREAM_ID.fullmatch(stream_id)
                or type(ready) is not int or not 0 <= ready <= MAX_PAGE):
            raise LibraryError("Invalid Telegram feed head")
        return stream_id, ready

    def read_page(self, scope: str, page: int) -> list[tuple[str, dict[str, Any]]]:
        entries: list[tuple[str, dict[str, Any]]] = []
        try:
            with open_feed(self.root, scope, f"{page:08d}.jsonl") as stream:
                for _ in range(MAX_ROWS):
                    line = stream.readline(MAX_ROW_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_ROW_BYTES or not line.endswith(b"\n"):
                        raise LibraryError("Telegram receipt exceeds its byte limit or has an incomplete line")
                    entries.append(receipt_origin(json_object(line), scope))
                if stream.read(1):
                    raise LibraryError("Telegram page exceeds its row limit")
            if not entries:
                raise LibraryError("Empty Telegram page")
        except (LibraryError, ValueError, RecursionError) as exc:
            detail = str(exc) if isinstance(exc, LibraryError) else "Invalid Telegram receipt JSON"
            raise BadPage(f"Row {len(entries) + 1}: {detail}") from exc
        return entries

    def register(self, scope: str, page: int, oid: str, origin: dict[str, Any], *, now: datetime) -> None:
        incoming = origin["telegram"]
        message = incoming["message"]
        trial = self.trial_origin(incoming["channel"], message, origin["size"], incoming["sha256"])
        if trial is not None:
            if self.catalog.get("origins", oid) is not None:
                raise BadPage(f"Historical trial and Telegram origin both exist for message {message}")
            return

        def repeat(existing: dict[str, Any], _proposed: dict[str, Any]) -> None:
            saved = existing.get("telegram", {})
            if (existing.get("size") != origin["size"]
                    or any(saved.get(field) != incoming[field] for field in ("channel", "topic", "message", "sha256"))
                    or "topic" not in saved):
                raise BadPage(f"Telegram identity or bytes conflict for message {message}")
            if incoming.get("index_title") is not None and incoming["index_title"] != saved.get("index_title"):
                self.report(scope, page, "degraded", "identity_conflict", f"Index title differs for message {message}")

        submit_origin(self.catalog, oid, origin, at=now, repeat=repeat)

    def trial_origin(self, channel: int, message: int, size: int, checksum: str) -> str | None:
        proof = TRIALS.get((channel, message))
        if proof is None:
            return None
        prefix = f"external:telegram/{channel}_{message}"
        rows = self.catalog.backend().external_folder_origins(prefix + "/")
        if not rows:
            return None
        legacy_id, legacy = rows[0]
        path = legacy["path"]
        if (len(rows) != 1 or proof != digest({"path": path, "size": size, "sha256": checksum})
                or legacy.get("telegram") is not None or legacy.get("size") != size or legacy.get("sha256") != checksum
                or legacy_id != digest({"external": path, "size": size, "mtime_ns": legacy.get("mtime_ns")})):
            raise BadPage(f"Historical Telegram trial conflicts for message {message}")
        return str(legacy_id)

    @staticmethod
    def report(scope: str, page: int, code: str, reason: str, detail: str) -> None:
        emit(service="collector", stage="diagnostics", queue="discovery", code=code, reason=reason,
             refs=[{"kind": "source", "id": digest({"telegram_scope": scope})}],
             detail=f"Telegram {scope} page {page}: {detail}")
