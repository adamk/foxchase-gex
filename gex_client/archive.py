"""Durable, local storage for computed GEX snapshots."""

from __future__ import annotations

import json
import hashlib
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")
SCHEMA_VERSION = 2
_write_lock = threading.Lock()


def archive_root() -> Path:
    configured = os.getenv("FOXCHASE_GEX_DATA_DIR", "~/.foxchase-gex/archive")
    return Path(configured).expanduser()


def _validated_symbol(symbol: str) -> str:
    value = symbol.upper().strip()
    if value not in {"SPX", "NDX"}:
        raise ValueError("supported symbols are SPX and NDX")
    return value


def _validated_day(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc


def _day_path(symbol: str, day: str) -> Path:
    return archive_root() / _validated_symbol(symbol) / f"{_validated_day(day)}.jsonl"


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _frame_count(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("r", encoding="utf-8") as stream:
        return sum(1 for line in stream if line.strip())


def archive_snapshot(
    symbol: str,
    result: dict,
    captured_at: Optional[datetime] = None,
    *,
    causal_input: Optional[dict] = None,
    provenance: Optional[dict] = None,
    request_timestamp: Optional[str] = None,
    response_timestamp: Optional[str] = None,
) -> Path:
    """Append one computed result, rejecting malformed or mismatched payloads."""
    display_symbol = _validated_symbol(symbol)
    if not isinstance(result, dict) or not isinstance(result.get("strikes"), list):
        raise ValueError("computed result must contain a strikes list")
    returned_symbol = str(result.get("display_symbol", display_symbol)).upper().strip()
    if returned_symbol != display_symbol:
        raise ValueError("computed result symbol does not match archive symbol")

    stamp = (captured_at or datetime.now(NY)).astimezone(NY)
    day = stamp.date().isoformat()
    destination = _day_path(display_symbol, day)
    destination.parent.mkdir(parents=True, exist_ok=True)
    causal_input = dict(causal_input or {})
    provenance = dict(provenance or {})
    if causal_input:
        provenance["input_content_hash"] = _canonical_hash(causal_input)
    record = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": stamp.isoformat(timespec="seconds"),
        "request_timestamp": request_timestamp,
        "response_timestamp": response_timestamp,
        "symbol": display_symbol,
        "frame_index": _frame_count(destination),
        "causal_input": causal_input,
        "provenance": provenance,
        "result": result,
    }
    record["frame_content_hash"] = _canonical_hash(record)
    encoded = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"

    with _write_lock:
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    return destination


def validate_frame_record(record: dict) -> bool:
    expected = str(record.get("frame_content_hash", ""))
    if len(expected) != 64:
        return False
    body = dict(record)
    body.pop("frame_content_hash", None)
    return _canonical_hash(body) == expected


def list_sessions(symbol: str) -> list[dict]:
    display_symbol = _validated_symbol(symbol)
    root = archive_root() / display_symbol
    if not root.is_dir():
        return []
    sessions = []
    for path in sorted(root.glob("????-??-??.jsonl"), reverse=True):
        try:
            day = _validated_day(path.stem)
            captures = sum(1 for _ in path.open("r", encoding="utf-8"))
            stat = path.stat()
        except (OSError, ValueError, UnicodeError):
            continue
        if captures:
            sessions.append({"date": day, "captures": captures, "bytes": stat.st_size})
    return sessions


def read_day(symbol: str, day: str) -> list[dict]:
    source = _day_path(symbol, day)
    if not source.is_file():
        return []
    records = []
    try:
        with source.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(record, dict)
                    and isinstance(record.get("result"), dict)
                    and isinstance(record["result"].get("strikes"), list)
                ):
                    records.append(record)
    except OSError:
        return []
    return records


def timeline(symbol: str, day: str) -> list[dict]:
    points = []
    for index, record in enumerate(read_day(symbol, day)):
        result = record["result"]
        patterns = result.get("patterns") if isinstance(result.get("patterns"), dict) else {}
        points.append(
            {
                "index": index,
                "captured_at": record.get("captured_at"),
                "spot": result.get("spot"),
                "read": patterns.get("read_title", patterns.get("primary")),
            }
        )
    return points


def snapshot(symbol: str, day: str, index: int) -> Optional[dict]:
    records = read_day(symbol, day)
    if not records:
        return None
    resolved = index if index >= 0 else len(records) + index
    if resolved < 0 or resolved >= len(records):
        return None
    record = records[resolved]
    result = dict(record["result"])
    result["historical"] = True
    result["historical_date"] = _validated_day(day)
    result["historical_index"] = resolved
    result["captured_at"] = record.get("captured_at")
    return result


def verify_archive_mount() -> None:
    """Optionally require the archive to live beneath a real mounted filesystem."""
    required_mount = os.getenv("FOXCHASE_GEX_REQUIRED_MOUNT", "").strip()
    if not required_mount:
        return
    mount = Path(required_mount).expanduser().resolve()
    root = archive_root().resolve()
    try:
        root.relative_to(mount)
    except ValueError as exc:
        raise RuntimeError(f"archive path {root} is outside required mount {mount}") from exc
    if not mount.is_mount():
        raise RuntimeError(f"required archive mount is unavailable: {mount}")
