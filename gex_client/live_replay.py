"""Current-session replay from the existing local, provenance-bearing archive.

The collector's schema-v2 JSONL files are the only ingest source in the public
client. This reader never writes archive, cursor, or token state.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from gex_client.archive import archive_root, validate_frame_record

ET = ZoneInfo("America/New_York")
SYMBOLS = {"SPX", "NDX"}
MAX_RECORDS = 820
MAX_LINE_BYTES = 96 * 1024
MAX_ARCHIVE_BYTES = MAX_RECORDS * MAX_LINE_BYTES


class ReplayUnavailable(ValueError):
    """A current-session capture cannot be verified safely."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _symbol(value: str) -> str:
    if value not in SYMBOLS:
        raise ValueError("supported symbols are SPX and NDX")
    return value


def _records(symbol: str, now: datetime | None = None) -> tuple[str, list[dict]]:
    symbol = _symbol(symbol)
    current = now or _now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("current time must include a timezone")
    day = current.astimezone(ET).date().isoformat()
    path = archive_root() / symbol / (day + ".jsonl")
    if path.is_symlink() or path.parent.is_symlink():
        raise ReplayUnavailable("replay archive path is unsafe")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return day, []
    except OSError as exc:
        raise ReplayUnavailable("replay archive unavailable") from exc

    rows: list[dict] = []
    seen: dict[str, str] = {}
    try:
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ARCHIVE_BYTES:
                raise ReplayUnavailable("replay archive exceeds bound")
            for index, line in enumerate(stream, 1):
                if index > MAX_RECORDS or len(line) > MAX_LINE_BYTES:
                    raise ReplayUnavailable("replay session exceeds bound")
                if not line.endswith(b"\n"):
                    break  # The collector may be appending the next complete line.
                try:
                    record = json.loads(line)
                    if (not isinstance(record, dict) or record.get("schema_version") != 2
                            or record.get("symbol") != symbol
                            or not validate_frame_record(record)):
                        raise ValueError("invalid frame")
                    captured_at = record["captured_at"]
                    if not isinstance(captured_at, str):
                        raise ValueError("invalid capture time")
                    stamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
                    if (stamp.tzinfo is None or stamp.utcoffset() is None
                            or stamp.astimezone(ET).date().isoformat() != day
                            or stamp.astimezone(timezone.utc) > current.astimezone(timezone.utc)):
                        raise ValueError("invalid capture time")
                    result = record["result"]
                    if (not isinstance(result, dict)
                            or result.get("display_symbol", symbol) != symbol
                            or not isinstance(result.get("strikes"), list)):
                        raise ValueError("invalid computed result")
                except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                    raise ReplayUnavailable("replay capture verification failed") from exc
                digest = hashlib.sha256(line).hexdigest()
                if captured_at in seen:
                    if seen[captured_at] != digest:
                        raise ReplayUnavailable("conflicting capture timestamp")
                    continue
                seen[captured_at] = digest
                rows.append({"cursor": index, "captured_at": captured_at,
                             "record_sha256": digest,
                             "frame_content_hash": record["frame_content_hash"],
                             "result": result})
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ReplayUnavailable("replay archive changed during read")
    except OSError as exc:
        raise ReplayUnavailable("replay archive unavailable") from exc
    return day, rows


def timeline(symbol: str, after: str | None = None, *, now: datetime | None = None) -> dict:
    if after is not None and (not after.isdigit() or len(after) > 18):
        raise ValueError("invalid replay cursor")
    day, rows = _records(symbol, now)
    cursor = int(after) if after is not None else 0
    return {"symbol": symbol, "session_date": day,
            "points": [{key: row[key] for key in ("cursor", "captured_at",
                                               "record_sha256", "frame_content_hash")}
                       for row in rows if row["cursor"] > cursor]}


def snapshot(symbol: str, captured_at: str, *, now: datetime | None = None) -> dict:
    if len(captured_at) > 64:
        raise ValueError("invalid replay timestamp")
    day, rows = _records(symbol, now)
    for row in rows:
        if row["captured_at"] == captured_at:
            result = dict(row["result"])
            result.update(captured_at=captured_at, replay=True,
                          replay_session_date=day)
            result["_replay"] = {
                "record_sha256": row["record_sha256"],
                "frame_content_hash": row["frame_content_hash"],
                "kind": "originally recorded calculated output",
                "recomputed": False,
            }
            return result
    raise LookupError("replay snapshot unavailable")
