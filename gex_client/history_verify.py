"""Read-only verification and health reporting for schema-v2 GEX archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from gex_client.archive import archive_root


NY = ZoneInfo("America/New_York")
SCHEMA_VERSION = 2
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_RECORDS = 2_000_000
DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_ISSUES = 1_000
_SESSION_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
_ALLOWED_ROOT_DIRS = {"SPX", "NDX", "audit", "status"}


class ArchiveError(RuntimeError):
    """The archive cannot be safely scanned."""


class _CallbackIOError(Exception):
    def __init__(self, error: OSError):
        self.error = error


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _absolute(path: os.PathLike | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _reject_symlink_components(path: Path) -> None:
    """Reject symlinks in the selected path, including its existing parents."""
    for component in reversed((path, *path.parents)):
        try:
            mode = os.lstat(component).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ArchiveError("archive path cannot be inspected") from exc
        if stat.S_ISLNK(mode):
            raise ArchiveError("archive path contains a symbolic link")


def _parse_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(NY)


def _no_duplicate_object_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("non-finite JSON value")


def _load_calendar(path: Optional[os.PathLike | str]) -> dict:
    if path is None:
        return {"holidays": set(), "trading_sessions": None, "early_closes": {}}
    calendar_path = _absolute(path)
    _reject_symlink_components(calendar_path)
    try:
        fd = os.open(
            calendar_path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ArchiveError("calendar file is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArchiveError("calendar path must be a regular file")
        if metadata.st_size > 1_000_000:
            raise ArchiveError("calendar file exceeds the 1 MB limit")
        chunks = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise ArchiveError("calendar file changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if after.st_size != metadata.st_size or after.st_mtime_ns != metadata.st_mtime_ns:
            raise ArchiveError("calendar file changed while being read")
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_object_keys)
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ArchiveError("calendar file is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ArchiveError("calendar file must contain a JSON object")

    def dates(name):
        entries = value.get(name, [])
        if not isinstance(entries, list):
            raise ArchiveError(f"calendar {name} must be a list")
        checked = set()
        for item in entries:
            try:
                checked.add(date.fromisoformat(item).isoformat())
            except (TypeError, ValueError) as exc:
                raise ArchiveError(f"calendar {name} contains an invalid date") from exc
        return checked

    holidays = dates("holidays")
    sessions = dates("trading_sessions") if "trading_sessions" in value else None
    early_closes = value.get("early_closes", {})
    if not isinstance(early_closes, dict):
        raise ArchiveError("calendar early_closes must be an object")
    checked_closes = {}
    for day, clock in early_closes.items():
        try:
            checked_day = date.fromisoformat(day).isoformat()
            parsed_time = time.fromisoformat(clock)
        except (TypeError, ValueError) as exc:
            raise ArchiveError("calendar early_closes contains an invalid date or time") from exc
        if parsed_time.tzinfo is not None:
            raise ArchiveError("calendar early-close times must be local HH:MM values")
        checked_closes[checked_day] = parsed_time
    return {"holidays": holidays, "trading_sessions": sessions, "early_closes": checked_closes}


def _session_close(day: str, default_close: time, calendar: dict) -> time:
    return calendar["early_closes"].get(day, default_close)


def _record_errors(record: object, symbol: str, day: str) -> tuple[list[tuple[str, str]], Optional[datetime]]:
    errors: list[tuple[str, str]] = []
    if not isinstance(record, dict):
        return [("invalid_record", "record must be a JSON object")], None
    required = {
        "schema_version", "captured_at", "request_timestamp", "response_timestamp",
        "symbol", "frame_index", "causal_input", "provenance", "result", "frame_content_hash",
    }
    missing = sorted(required - set(record))
    if missing:
        errors.append(("missing_fields", "record is missing required fields"))
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append(("unsupported_schema", "record schema_version is not 2"))
    if record.get("symbol") != symbol:
        errors.append(("symbol_mismatch", "record symbol does not match its directory"))

    captured = _parse_timestamp(record.get("captured_at"))
    if captured is None:
        errors.append(("invalid_timestamp", "captured_at must be an ISO timestamp with a timezone"))
    elif captured.date().isoformat() != day:
        errors.append(("session_date_mismatch", "captured_at date does not match the session filename"))

    for key in ("request_timestamp", "response_timestamp"):
        value = record.get(key)
        if value is not None and _parse_timestamp(value) is None:
            errors.append(("invalid_timestamp", f"{key} must be null or an ISO timestamp with a timezone"))

    frame_index = record.get("frame_index")
    if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
        errors.append(("invalid_frame_index", "frame_index must be a non-negative integer"))

    causal = record.get("causal_input")
    provenance = record.get("provenance")
    result = record.get("result")
    if not isinstance(causal, dict):
        errors.append(("invalid_causal_input", "causal_input must be an object"))
    if not isinstance(provenance, dict):
        errors.append(("invalid_provenance", "provenance must be an object"))
    if not isinstance(result, dict) or not isinstance(result.get("strikes"), list):
        errors.append(("invalid_result", "result must be an object containing a strikes list"))
    else:
        result_symbol = result.get("display_symbol")
        if result_symbol is not None and str(result_symbol).upper() != symbol:
            errors.append(("symbol_mismatch", "computed result symbol does not match its archive"))

    if isinstance(causal, dict) and causal.get("symbol") is not None:
        if str(causal.get("symbol")).upper() not in {symbol, f"${symbol}"}:
            errors.append(("symbol_mismatch", "causal_input symbol does not match its archive"))
    if isinstance(causal, dict) and isinstance(provenance, dict) and causal:
        expected_input_hash = provenance.get("input_content_hash")
        try:
            matches = expected_input_hash == _canonical_hash(causal)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            errors.append(("input_hash_mismatch", "causal_input does not match its provenance hash"))

    expected_frame_hash = record.get("frame_content_hash")
    if not isinstance(expected_frame_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_frame_hash):
        errors.append(("invalid_frame_hash", "frame_content_hash must be a lowercase SHA-256 digest"))
    else:
        body = dict(record)
        body.pop("frame_content_hash", None)
        try:
            matches = _canonical_hash(body) == expected_frame_hash
        except (TypeError, ValueError):
            matches = False
        if not matches:
            errors.append(("frame_hash_mismatch", "record contents do not match frame_content_hash"))
    return errors, captured


def verify_archive(
    archive_dir: Optional[os.PathLike | str] = None,
    *,
    symbol: Optional[str] = None,
    cadence_seconds: int = 60,
    session_open: str = "09:30",
    session_close: str = "16:00",
    calendar_file: Optional[os.PathLike | str] = None,
    required_mount: Optional[os.PathLike | str] = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_records: int = DEFAULT_MAX_RECORDS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    on_record: Optional[Callable[[str, str, int, dict], None]] = None,
) -> dict:
    """Scan SPX/NDX session files without writing to the archive.

    A callback receives only syntactically parsed object records; callers must
    still honor the returned status before activating any derived artifacts.
    """
    if cadence_seconds < 1 or min(max_files, max_records, max_file_bytes, max_line_bytes, max_total_bytes) < 1:
        raise ArchiveError("scan limits and cadence must be positive")
    try:
        open_time = time.fromisoformat(session_open)
        close_time = time.fromisoformat(session_close)
    except ValueError as exc:
        raise ArchiveError("session open and close must use HH:MM") from exc
    if open_time.tzinfo is not None or close_time.tzinfo is not None or open_time >= close_time:
        raise ArchiveError("session open must precede close and use local HH:MM")
    if symbol is not None:
        symbol = symbol.upper().strip()
        if symbol not in {"SPX", "NDX"}:
            raise ArchiveError("supported symbols are SPX and NDX")
    calendar = _load_calendar(calendar_file)
    if any(not open_time < early_close <= close_time for early_close in calendar["early_closes"].values()):
        raise ArchiveError("calendar early-close times must fall after session open and no later than session close")
    root = _absolute(archive_dir if archive_dir is not None else archive_root())
    _reject_symlink_components(root)
    try:
        root_mode = os.lstat(root).st_mode
    except OSError as exc:
        raise ArchiveError("archive directory is unavailable") from exc
    if not stat.S_ISDIR(root_mode):
        raise ArchiveError("archive root must be a directory")

    if required_mount is not None:
        mount = _absolute(required_mount)
        _reject_symlink_components(mount)
        try:
            mount_mode = os.lstat(mount).st_mode
        except OSError as exc:
            raise ArchiveError("required archive mount is unavailable") from exc
        if not stat.S_ISDIR(mount_mode) or not os.path.ismount(mount):
            raise ArchiveError("required archive mount is not mounted")
        try:
            root.relative_to(mount)
        except ValueError as exc:
            raise ArchiveError("archive directory is outside the required mount") from exc

    issues: list[dict] = []
    issue_counts = {"error": 0, "warning": 0}
    issue_overflow = False

    def issue(code: str, message: str, severity: str = "error", **fields) -> None:
        nonlocal issue_overflow
        issue_counts[severity] += 1
        if len(issues) < MAX_ISSUES:
            entry = {"code": code, "severity": severity, "message": message}
            entry.update(fields)
            issues.append(entry)
        elif not issue_overflow:
            issue_overflow = True
            issues.append({"code": "issues_truncated", "severity": "warning", "message": "additional findings omitted"})

    selected_symbols = [symbol] if symbol else ["SPX", "NDX"]
    symbols_report = {}
    all_sessions: dict[str, dict[str, dict]] = {name: {} for name in selected_symbols}
    archive_bytes = 0
    bytes_scanned = 0
    files_found = 0
    files_scanned = 0
    records_scanned = 0
    valid_records = 0
    synthetic_records = 0
    non_synthetic_records = 0
    scan_complete = True
    record_limit_hit = False

    try:
        root_entries = os.scandir(root)
    except OSError as exc:
        raise ArchiveError("archive directory cannot be read") from exc
    with root_entries:
        root_entry_count = 0
        for entry in root_entries:
            root_entry_count += 1
            if root_entry_count > max(100, max_files + 32):
                issue("resource_limit", "archive root entry-count limit reached")
                scan_complete = False
                break
            if entry.name in _ALLOWED_ROOT_DIRS:
                if entry.is_symlink():
                    issue("symlink_entry", "archive root contains a symbolic link", name=entry.name)
                elif entry.name in (set(selected_symbols) | {"audit", "status"}) and not entry.is_dir(follow_symlinks=False):
                    issue("unexpected_entry", "symbol entry is not a directory", name=entry.name)
            else:
                issue("unexpected_entry", "archive root contains an unexpected entry", name=entry.name)

    for display_symbol in selected_symbols:
        symbol_root = root / display_symbol
        try:
            symbol_mode = os.lstat(symbol_root).st_mode
        except FileNotFoundError:
            issue("missing_symbol_directory", "symbol archive directory is missing", "warning", symbol=display_symbol)
            symbols_report[display_symbol] = {
                "session_count": 0, "capture_count": 0, "archive_bytes": 0,
                "latest_successful_capture": None, "last_archived_timestamp": None,
                "available_sessions": [], "capture_gaps": 0, "invalid_sessions": [],
                "incomplete_sessions": [],
            }
            continue
        except OSError as exc:
            issue("unreadable_symbol_directory", "symbol archive directory cannot be inspected", symbol=display_symbol)
            scan_complete = False
            continue
        if stat.S_ISLNK(symbol_mode):
            issue("symlink_entry", "symbol archive directory is a symbolic link", symbol=display_symbol)
            continue
        if not stat.S_ISDIR(symbol_mode):
            issue("unexpected_entry", "symbol archive entry is not a directory", symbol=display_symbol)
            continue
        try:
            entries = os.scandir(symbol_root)
        except OSError:
            issue("unreadable_symbol_directory", "symbol archive directory cannot be read", symbol=display_symbol)
            scan_complete = False
            continue
        session_entries = []
        with entries:
            directory_entry_count = 0
            for entry in entries:
                directory_entry_count += 1
                if directory_entry_count > max(100, max_files + 32):
                    issue("resource_limit", "symbol directory entry-count limit reached", symbol=display_symbol)
                    scan_complete = False
                    break
                if entry.is_symlink():
                    issue("symlink_entry", "symbol archive contains a symbolic link", symbol=display_symbol, name=entry.name)
                    continue
                if not _SESSION_NAME.fullmatch(entry.name):
                    issue("unexpected_entry", "symbol archive contains an unexpected entry", symbol=display_symbol, name=entry.name)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    issue("unexpected_entry", "session path is not a regular file", symbol=display_symbol, name=entry.name)
                    continue
                try:
                    parsed_day = date.fromisoformat(entry.name[:-6]).isoformat()
                    entry_size = entry.stat(follow_symlinks=False).st_size
                except (OSError, ValueError):
                    issue("unreadable_session", "session file metadata cannot be read", symbol=display_symbol, name=entry.name)
                    scan_complete = False
                    continue
                files_found += 1
                archive_bytes += entry_size
                if files_found <= max_files:
                    session_entries.append((parsed_day, symbol_root / entry.name, entry_size))
                else:
                    issue("resource_limit", "archive file-count limit reached", symbol=display_symbol)
                    scan_complete = False
                    break
        session_entries.sort()

        symbol_bytes = sum(item[2] for item in session_entries)
        symbol_gaps = 0
        symbol_capture_count = 0
        latest = None
        invalid_sessions = []
        incomplete_sessions = []
        for day, path, expected_size in session_entries:
            if files_scanned >= max_files or record_limit_hit:
                break
            files_scanned += 1
            session_issues_before = issue_counts.copy()
            session = {
                "date": day, "capture_count": 0, "parsed_record_count": 0,
                "start_timestamp": None, "end_timestamp": None,
                "schema_version": SCHEMA_VERSION, "integrity": "valid",
                "capture_gaps": [], "bytes": 0,
            }
            session_state = {
                "seen_indexes": {}, "seen_timestamps": {},
                "last_index": None, "last_timestamp": None,
            }
            try:
                fd = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError:
                issue("unreadable_session", "session file cannot be opened safely", symbol=display_symbol, session=day)
                invalid_sessions.append(day)
                scan_complete = False
                continue
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    issue("unexpected_entry", "session path is not a regular file", symbol=display_symbol, session=day)
                    invalid_sessions.append(day)
                    scan_complete = False
                    continue
                session["bytes"] = expected_size
                if before.st_size != expected_size:
                    issue("source_changed_during_scan", "session file changed before it was scanned", symbol=display_symbol, session=day)
                    scan_complete = False
                    continue
                if before.st_size > max_file_bytes:
                    issue("resource_limit", "session file exceeds the configured size limit", symbol=display_symbol, session=day)
                    invalid_sessions.append(day)
                    scan_complete = False
                    continue
                if bytes_scanned + before.st_size > max_total_bytes:
                    issue("resource_limit", "archive exceeds the configured total-byte limit", symbol=display_symbol, session=day)
                    scan_complete = False
                    break
                bytes_scanned += before.st_size

                line_buffer = bytearray()
                line_number = 0
                discarding_long_line = False
                consumed = 0
                def consume_line(raw_line: bytes, number: int) -> None:
                    nonlocal records_scanned, valid_records, synthetic_records, non_synthetic_records, latest, symbol_capture_count, symbol_gaps, record_limit_hit, scan_complete
                    if not raw_line.strip():
                        issue("blank_line", "session contains an empty line", "warning", symbol=display_symbol, session=day, line=number)
                        return
                    if records_scanned >= max_records:
                        if not record_limit_hit:
                            issue("resource_limit", "archive record-count limit reached", symbol=display_symbol, session=day, line=number)
                        record_limit_hit = True
                        scan_complete = False
                        return
                    records_scanned += 1
                    session["parsed_record_count"] += 1
                    try:
                        record = json.loads(
                            raw_line.decode("utf-8"),
                            object_pairs_hook=_no_duplicate_object_keys,
                            parse_constant=_reject_json_constant,
                        )
                    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
                        issue("malformed_json", "line is not a valid single JSON record", symbol=display_symbol, session=day, line=number)
                        return
                    errors, captured = _record_errors(record, display_symbol, day)
                    if errors:
                        for code, message in errors:
                            issue(code, message, symbol=display_symbol, session=day, line=number)
                    if not isinstance(record, dict):
                        return
                    index = record.get("frame_index")
                    if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
                        if index in session_state["seen_indexes"]:
                            issue("duplicate_frame_index", "frame_index occurs more than once", symbol=display_symbol, session=day, line=number)
                        elif session_state["last_index"] is not None and index > session_state["last_index"] + 1:
                            issue("missing_frame_index", "one or more frame indexes are missing", "warning", symbol=display_symbol, session=day, line=number)
                        if session_state["last_index"] is not None and index < session_state["last_index"]:
                            issue("nonmonotonic_frame_index", "frame indexes move backwards", symbol=display_symbol, session=day, line=number)
                        session_state["seen_indexes"][index] = number
                        session_state["last_index"] = index
                    if captured is not None:
                        stamp_key = captured.isoformat(timespec="microseconds")
                        if stamp_key in session_state["seen_timestamps"]:
                            issue("duplicate_capture", "captured_at timestamp occurs more than once", symbol=display_symbol, session=day, line=number)
                        if session_state["last_timestamp"] is not None:
                            previous = session_state["last_timestamp"]
                            if captured <= previous:
                                issue("nonmonotonic_timestamp", "captured_at timestamps are not strictly increasing", symbol=display_symbol, session=day, line=number)
                            elif captured.date() == previous.date():
                                delta = (captured - previous).total_seconds()
                                expected_close = _session_close(day, close_time, calendar)
                                lower = datetime.combine(captured.date(), open_time, NY)
                                upper = datetime.combine(captured.date(), expected_close, NY)
                                trading_days = calendar["trading_sessions"]
                                collection_day = (
                                    captured.weekday() < 5
                                    and day not in calendar["holidays"]
                                    and (trading_days is None or day in trading_days)
                                )
                                clipped_start = max(previous, lower)
                                clipped_end = min(captured, upper)
                                collection_delta = max(0.0, (clipped_end - clipped_start).total_seconds())
                                if collection_day and collection_delta >= cadence_seconds * 1.5:
                                    missed = max(1, round(collection_delta / cadence_seconds) - 1)
                                    gap = {
                                        "from": clipped_start.isoformat(timespec="seconds"),
                                        "to": clipped_end.isoformat(timespec="seconds"),
                                        "estimated_missing_captures": missed,
                                        "classification": "capture_gap_possible_outage",
                                    }
                                    session["capture_gaps"].append(gap)
                                    symbol_gaps += 1
                                    issue("capture_gap", "expected capture interval is missing; no data was inferred", "warning", symbol=display_symbol, session=day, line=number, **gap)
                                if delta >= cadence_seconds * 1.5 and (
                                    not collection_day or previous < lower or captured > upper
                                ):
                                    issue(
                                        "noncollection_interval",
                                        "timestamp separation overlaps a configured noncollection period",
                                        "warning", symbol=display_symbol, session=day, line=number,
                                        classification="noncollection_period",
                                    )
                        session_state["seen_timestamps"][stamp_key] = number
                        session_state["last_timestamp"] = captured
                    if errors:
                        return
                    captured_clock = captured.timetz().replace(tzinfo=None)
                    expected_close = _session_close(day, close_time, calendar)
                    trading_days = calendar["trading_sessions"]
                    if (
                        captured.weekday() >= 5
                        or day in calendar["holidays"]
                        or (trading_days is not None and day not in trading_days)
                        or not (open_time <= captured_clock <= expected_close)
                    ):
                        issue(
                            "capture_during_noncollection_period",
                            "capture timestamp falls outside the configured weekday collection window",
                            "warning", symbol=display_symbol, session=day, line=number,
                            classification="noncollection_period",
                        )
                    valid_records += 1
                    session["capture_count"] += 1
                    symbol_capture_count += 1
                    if session["start_timestamp"] is None:
                        session["start_timestamp"] = record["captured_at"]
                    session["end_timestamp"] = record["captured_at"]
                    latest = max(latest or captured, captured)
                    provenance = record.get("provenance", {})
                    causal_input = record.get("causal_input", {})
                    if (
                        provenance.get("data_class") == "synthetic"
                        and provenance.get("provider") == "synthetic_fixture"
                        and isinstance(causal_input, dict)
                        and causal_input.get("synthetic_fixture") is True
                    ):
                        synthetic_records += 1
                    else:
                        non_synthetic_records += 1
                    if on_record is not None:
                        try:
                            on_record(display_symbol, day, number, record)
                        except OSError as exc:
                            raise _CallbackIOError(exc) from exc

                try:
                    while consumed < before.st_size:
                        chunk = os.read(fd, min(64 * 1024, before.st_size - consumed))
                        if not chunk:
                            issue("source_changed_during_scan", "session file became shorter while it was scanned", symbol=display_symbol, session=day)
                            scan_complete = False
                            break
                        consumed += len(chunk)
                        start = 0
                        while start < len(chunk) and not record_limit_hit:
                            newline = chunk.find(b"\n", start)
                            stop = len(chunk) if newline < 0 else newline
                            piece = chunk[start:stop]
                            if not discarding_long_line:
                                line_buffer.extend(piece)
                                if len(line_buffer) > max_line_bytes:
                                    line_number += 1
                                    issue("resource_limit", "JSONL record exceeds the configured line-size limit", symbol=display_symbol, session=day, line=line_number)
                                    line_buffer.clear()
                                    discarding_long_line = True
                            if newline >= 0:
                                line_number += 1
                                if not discarding_long_line:
                                    consume_line(bytes(line_buffer), line_number)
                                line_buffer.clear()
                                discarding_long_line = False
                                start = newline + 1
                            else:
                                start = len(chunk)
                    if line_buffer or discarding_long_line:
                        if discarding_long_line:
                            issue("truncated_final_record", "final JSONL record is oversized and lacks a newline", symbol=display_symbol, session=day, line=line_number + 1)
                        else:
                            issue("truncated_final_record", "final JSONL record is incomplete because it lacks a newline", symbol=display_symbol, session=day, line=line_number + 1)
                    after = os.fstat(fd)
                    if after.st_size < before.st_size or (after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns):
                        issue("source_changed_during_scan", "session file changed while it was scanned", symbol=display_symbol, session=day)
                        scan_complete = False
                    elif after.st_size > before.st_size:
                        session["concurrent_append_bytes"] = after.st_size - before.st_size
                except _CallbackIOError as exc:
                    raise exc.error
                except OSError:
                    issue("unreadable_session", "session file could not be read completely", symbol=display_symbol, session=day)
                    scan_complete = False
            finally:
                os.close(fd)

            if not session["parsed_record_count"]:
                issue("empty_session", "session file contains no records", "warning", symbol=display_symbol, session=day)
            session_issue_errors = issue_counts["error"] - session_issues_before["error"]
            session_issue_warnings = issue_counts["warning"] - session_issues_before["warning"]
            if session_issue_errors:
                session["integrity"] = "invalid"
                invalid_sessions.append(day)
            elif session_issue_warnings:
                session["integrity"] = "incomplete"
                incomplete_sessions.append(day)
            all_sessions[display_symbol][day] = session
            if record_limit_hit:
                break

        if calendar["trading_sessions"] is not None:
            for expected_day in sorted(calendar["trading_sessions"] - calendar["holidays"]):
                if expected_day not in all_sessions[display_symbol]:
                    issue("missing_trading_session", "calendar marks a trading session with no archive file", "warning", symbol=display_symbol, session=expected_day)
        session_list = [all_sessions[display_symbol][day] for day in sorted(all_sessions[display_symbol])]
        symbols_report[display_symbol] = {
            "session_count": len(session_list),
            "capture_count": symbol_capture_count,
            "archive_bytes": symbol_bytes,
            "latest_successful_capture": latest.isoformat(timespec="seconds") if latest else None,
            "last_archived_timestamp": latest.isoformat(timespec="seconds") if latest else None,
            "available_sessions": [item["date"] for item in session_list],
            "capture_gaps": symbol_gaps,
            "invalid_sessions": sorted(set(invalid_sessions)),
            "incomplete_sessions": sorted(set(incomplete_sessions)),
            "sessions": session_list,
        }

    if valid_records == 0:
        issue("no_valid_records", "archive contains no valid schema-v2 captures", "warning")
    if not scan_complete:
        status = "unavailable"
    elif issue_counts["error"]:
        status = "invalid"
    elif issue_counts["warning"]:
        status = "incomplete"
    else:
        status = "valid"
    integrity_ready = status == "valid" and valid_records > 0
    rights_status = "synthetic_claim_unverified" if synthetic_records == valid_records and valid_records else (
        "review_required" if non_synthetic_records else "unknown"
    )
    report = {
        "report_version": 1,
        "status": status,
        "archive_bytes": archive_bytes,
        "bytes_scanned": bytes_scanned,
        "files_found": files_found,
        "files_scanned": files_scanned,
        "records_scanned": records_scanned,
        "valid_captures": valid_records,
        "symbols": symbols_report,
        "last_archived_timestamp": max(
            (value["last_archived_timestamp"] for value in symbols_report.values() if value["last_archived_timestamp"]),
            default=None,
        ),
        "issues": issues,
        "issue_counts": issue_counts,
        "publisher_readiness": {
            "integrity": "ready" if integrity_ready else "blocked",
            "redistribution": rights_status,
            "ready": bool(integrity_ready and rights_status == "operator_attested_not_legal_clearance"),
        },
        "limits": {
            "max_files": max_files, "max_records": max_records,
            "max_file_bytes": max_file_bytes, "max_line_bytes": max_line_bytes,
            "max_total_bytes": max_total_bytes,
        },
    }
    return report


def _text_report(report: dict) -> str:
    lines = [
        f"Archive status: {report['status']}",
        f"Files: {report['files_scanned']} · captures: {report['valid_captures']} · storage: {report['archive_bytes']} bytes",
    ]
    for symbol, value in report["symbols"].items():
        latest = value["latest_successful_capture"] or "none"
        lines.append(
            f"{symbol}: latest={latest} · sessions={value['session_count']} · "
            f"captures={value['capture_count']} · gaps={value['capture_gaps']} · "
            f"bytes={value['archive_bytes']}"
        )
    lines.append(
        "Publisher readiness: "
        f"integrity={report['publisher_readiness']['integrity']} · "
        f"redistribution={report['publisher_readiness']['redistribution']}"
    )
    for item in report["issues"]:
        location = "/".join(str(item[key]) for key in ("symbol", "session", "line") if key in item)
        suffix = f" ({location})" if location else ""
        lines.append(f"{item['severity'].upper()} {item['code']}{suffix}: {item['message']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only validation and health report for SPX/NDX GEX archives")
    parser.add_argument("archive_dir", nargs="?", help="archive root; defaults to FOXCHASE_GEX_DATA_DIR")
    parser.add_argument("--symbol", choices=("SPX", "NDX"), help="scan one symbol")
    parser.add_argument("--cadence-seconds", type=int, default=int(os.getenv("FOXCHASE_GEX_CAPTURE_SECONDS", "60")))
    parser.add_argument("--session-open", default="09:30", help="local exchange collection start (HH:MM)")
    parser.add_argument("--session-close", default="16:00", help="local exchange collection end (HH:MM)")
    parser.add_argument("--calendar", dest="calendar_file", help="optional JSON calendar with holidays, trading_sessions, and early_closes")
    parser.add_argument("--required-mount", help="require archive to be beneath this mounted directory")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-line-bytes", type=int, default=DEFAULT_MAX_LINE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        report = verify_archive(
            args.archive_dir,
            symbol=args.symbol,
            cadence_seconds=args.cadence_seconds,
            session_open=args.session_open,
            session_close=args.session_close,
            calendar_file=args.calendar_file,
            required_mount=args.required_mount,
            max_files=args.max_files,
            max_records=args.max_records,
            max_file_bytes=args.max_file_bytes,
            max_line_bytes=args.max_line_bytes,
            max_total_bytes=args.max_total_bytes,
        )
    except ArchiveError as exc:
        if args.format == "json":
            print(json.dumps({"report_version": 1, "status": "unavailable", "error": str(exc)}, sort_keys=True))
        else:
            print(f"Archive unavailable: {exc}")
        return 2
    if args.format == "json":
        print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        print(_text_report(report))
    if report["status"] == "unavailable":
        return 2
    return 0 if report["status"] == "valid" else 1


if __name__ == "__main__":
    sys.exit(main())
