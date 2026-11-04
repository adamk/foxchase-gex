"""Generate deterministic, local-only historical publication generations."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Optional

from gex_client.archive import archive_root
from gex_client.history_verify import (
    ArchiveError, DEFAULT_MAX_TOTAL_BYTES, _absolute, _no_duplicate_object_keys,
    _reject_symlink_components, verify_archive,
)


FORMAT_VERSION = 1
DEFAULT_RESULT_FIELDS = ("display_symbol", "spot", "strikes", "structure")
SUPPORTED_RESULT_FIELDS = set(DEFAULT_RESULT_FIELDS)
STRIKE_FIELDS = ("strike", "gex", "call_gex", "put_gex")
STRUCTURE_FIELDS = ("net_gex", "gamma_flip", "call_wall", "put_wall")
GAP_WARNING_CODES = {"capture_gap", "missing_frame_index"}


class ExportError(RuntimeError):
    """The export cannot be generated safely."""


class ExportBlocked(ExportError):
    """Verified archive or data-rights conditions block publication."""


class ExportConfigurationError(ExportError):
    """Input or output configuration is unsafe or unavailable."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _paths_overlap(first: Path, second: Path) -> bool:
    left = os.path.normcase(os.path.abspath(first))
    right = os.path.normcase(os.path.abspath(second))
    try:
        common = os.path.commonpath((left, right))
    except ValueError:
        return False
    return common == left or common == right


def _read_rights_policy(path: Optional[os.PathLike | str]) -> Optional[dict]:
    if path is None:
        return None
    policy_path = _absolute(path)
    _reject_symlink_components(policy_path)
    try:
        fd = os.open(
            policy_path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ExportConfigurationError("rights policy must be a regular file")
            if metadata.st_size > 64 * 1024:
                raise ExportConfigurationError("rights policy exceeds the 64 KB limit")
            chunks = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    raise ExportConfigurationError("rights policy changed while being read")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            if after.st_size != metadata.st_size or after.st_mtime_ns != metadata.st_mtime_ns:
                raise ExportConfigurationError("rights policy changed while being read")
            value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_no_duplicate_object_keys)
        finally:
            os.close(fd)
    except ExportError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ExportConfigurationError("rights policy is unavailable or invalid JSON") from exc
    if not isinstance(value, dict):
        raise ExportConfigurationError("rights policy must be a JSON object")
    if value.get("policy_version") != 1 or value.get("redistribution_review") != "operator_attested_not_legal_clearance":
        raise ExportBlocked(
            "rights policy must record operator_attested_not_legal_clearance; "
            "this is an operator gate, not a legal rights determination"
        )
    fields = value.get("result_fields")
    if not isinstance(fields, list) or not fields or any(not isinstance(item, str) for item in fields):
        raise ExportConfigurationError("rights policy result_fields must be a non-empty string list")
    if len(set(fields)) != len(fields) or not set(fields) <= SUPPORTED_RESULT_FIELDS:
        raise ExportConfigurationError("rights policy selects unsupported or duplicate result fields")
    if "strikes" not in fields:
        raise ExportConfigurationError("rights policy must retain strikes for historical visualization")
    return {"result_fields": tuple(sorted(fields))}


def _is_synthetic(record: dict) -> bool:
    provenance = record.get("provenance")
    causal_input = record.get("causal_input")
    return bool(
        isinstance(provenance, dict)
        and provenance.get("provider") == "synthetic_fixture"
        and provenance.get("data_class") == "synthetic"
        and isinstance(causal_input, dict)
        and causal_input.get("synthetic_fixture") is True
    )


def _finite_number(value: object, field: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExportBlocked(f"computed field {field} is not numeric")
    if not math.isfinite(float(value)):
        raise ExportBlocked(f"computed field {field} is not finite")
    return value


def _project_result(result: dict, symbol: str, fields: tuple[str, ...]) -> dict:
    """Copy only the documented computed-result allowlist."""
    projected = {}
    selected = set(fields)
    if "display_symbol" in selected:
        value = result.get("display_symbol", symbol)
        if not isinstance(value, str) or value.upper() != symbol:
            raise ExportBlocked("computed result display_symbol does not match its archive")
        projected["display_symbol"] = symbol
    if "spot" in selected:
        if result.get("spot") is None:
            raise ExportBlocked("computed result is missing spot")
        projected["spot"] = _finite_number(result["spot"], "spot")
    if "strikes" in selected:
        strikes = result.get("strikes")
        if not isinstance(strikes, list) or not strikes:
            raise ExportBlocked("computed result strikes must be a non-empty list")
        safe_strikes = []
        for row in strikes:
            if not isinstance(row, dict) or row.get("strike") is None:
                raise ExportBlocked("computed result contains a malformed strike row")
            safe = {"strike": _finite_number(row["strike"], "strikes.strike")}
            for key in STRIKE_FIELDS[1:]:
                if row.get(key) is not None:
                    safe[key] = _finite_number(row[key], f"strikes.{key}")
            safe_strikes.append(safe)
        projected["strikes"] = safe_strikes
    if "structure" in selected and result.get("structure") is not None:
        structure = result["structure"]
        if not isinstance(structure, dict):
            raise ExportBlocked("computed result structure is not an object")
        safe_structure = {}
        for key in STRUCTURE_FIELDS:
            if structure.get(key) is not None:
                safe_structure[key] = _finite_number(structure[key], f"structure.{key}")
        if safe_structure:
            projected["structure"] = safe_structure
    return projected


def _readiness_issues(report: dict, allow_capture_gaps: bool) -> list[dict]:
    issues = report.get("issues", [])
    if report.get("valid_captures", 0) < 1:
        return [{"code": "no_valid_records", "severity": "error"}]
    blocked = []
    for item in issues:
        if item.get("severity") == "error":
            blocked.append(item)
        elif not (allow_capture_gaps and item.get("code") in GAP_WARNING_CODES):
            blocked.append(item)
    if report.get("issue_counts", {}).get("error", 0) > len([i for i in issues if i.get("severity") == "error"]):
        blocked.append({"code": "omitted_errors", "severity": "error"})
    return blocked


def _snapshot_signature(report: dict) -> tuple:
    sessions = []
    for symbol in sorted(report.get("symbols", {})):
        for session in report["symbols"][symbol].get("sessions", []):
            sessions.append((
                symbol,
                session["date"],
                session["bytes"],
                session["capture_count"],
                session["start_timestamp"],
                session["end_timestamp"],
                session["integrity"],
            ))
    return report.get("archive_bytes"), report.get("records_scanned"), tuple(sessions)


class _TimelineCollector:
    def __init__(
        self,
        fields: tuple[str, ...],
        stage: Optional[Path] = None,
        allow_non_synthetic: bool = False,
        allow_synthetic_fixtures: bool = False,
    ):
        self.fields = fields
        self.stage = stage
        self.allow_non_synthetic = allow_non_synthetic
        self.allow_synthetic_fixtures = allow_synthetic_fixtures
        self.sessions: dict[tuple[str, str], dict] = {}
        self._handle = None
        self._handle_key = None

    def _close_current(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None
            self._handle_key = None

    def add(self, symbol: str, day: str, _line: int, record: dict) -> None:
        if _is_synthetic(record) and not self.allow_synthetic_fixtures and not self.allow_non_synthetic:
            raise ExportBlocked(
                "synthetic markers are unauthenticated; use --allow-synthetic-fixtures "
                "only for known synthetic fixtures or provide a local operator attestation"
            )
        if not _is_synthetic(record) and not self.allow_non_synthetic:
            raise ExportBlocked(
                "non-synthetic archive requires a local operator attestation; "
                "this gate does not establish legal redistribution rights"
            )
        result = record.get("result")
        if not isinstance(result, dict):
            raise ExportBlocked("computed result is malformed")
        projected = _project_result(result, symbol, self.fields)
        key = (symbol, day)
        state = self.sessions.get(key)
        if state is None:
            state = {
                "symbol": symbol, "date": day, "capture_count": 0,
                "first_captured_at": None, "last_captured_at": None,
                "timeline_path": f"timelines/{symbol}/{day}.jsonl",
                "timeline_sha256": hashlib.sha256(), "timeline_bytes": 0,
            }
            self.sessions[key] = state
        timeline_record = {
            "symbol": symbol,
            "date": day,
            "index": state["capture_count"],
            "captured_at": record["captured_at"],
            "result": projected,
        }
        encoded = _canonical_json(timeline_record) + b"\n"
        state["timeline_sha256"].update(encoded)
        state["timeline_bytes"] += len(encoded)
        state["capture_count"] += 1
        state["first_captured_at"] = state["first_captured_at"] or record["captured_at"]
        state["last_captured_at"] = record["captured_at"]
        if self.stage is not None:
            if self._handle_key != key:
                self._close_current()
                destination = self.stage / state["timeline_path"]
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._handle = destination.open("xb")
                self._handle_key = key
            self._handle.write(encoded)

    def close(self) -> None:
        self._close_current()

    def session_metadata(self, report: dict) -> list[dict]:
        output = []
        for key in sorted(self.sessions):
            value = self.sessions[key]
            symbol, day = key
            source_session = next(
                (item for item in report["symbols"][symbol]["sessions"] if item["date"] == day),
                {},
            )
            finding_codes = sorted({
                issue["code"] for issue in report["issues"]
                if issue.get("symbol") == symbol and issue.get("session") == day
                and issue.get("severity") == "warning"
            })
            gap_count = sum(
                1 for issue in report["issues"]
                if issue.get("symbol") == symbol and issue.get("session") == day
                and issue.get("code") == "capture_gap"
            )
            value["timeline_sha256"] = value["timeline_sha256"].hexdigest()
            value["schema_version"] = source_session.get("schema_version", 2)
            value["integrity_status"] = "verified_with_gaps" if finding_codes else "verified"
            value["gap_count"] = gap_count
            value["finding_codes"] = finding_codes
            output.append(value)
        return output


def _check_output_layout(output: Path, create: bool = False) -> None:
    _reject_symlink_components(output)
    if output.exists():
        if not stat.S_ISDIR(os.lstat(output).st_mode):
            raise ExportConfigurationError("output path must be a directory")
        try:
            entries = os.scandir(output)
        except OSError as exc:
            raise ExportConfigurationError("output directory cannot be read") from exc
        allowed = {"current.json", "generations"}
        with entries:
            for entry in entries:
                if entry.name not in allowed:
                    raise ExportConfigurationError("output directory contains an unexpected entry")
                if entry.is_symlink():
                    raise ExportConfigurationError("output directory contains a symbolic link")
                if entry.name == "current.json" and not entry.is_file(follow_symlinks=False):
                    raise ExportConfigurationError("current.json is not a regular file")
                if entry.name == "generations" and not entry.is_dir(follow_symlinks=False):
                    raise ExportConfigurationError("publication directory entry is not a directory")
    if create:
        try:
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            generations = output / "generations"
            generations.mkdir(mode=0o700, exist_ok=True)
            if generations.is_symlink() or not generations.is_dir():
                raise ExportConfigurationError("publication directory is unsafe")
            staging = generations / ".staging"
            staging.mkdir(mode=0o700, exist_ok=True)
            if staging.is_symlink() or not staging.is_dir():
                raise ExportConfigurationError("publication staging directory is unsafe")
        except OSError as exc:
            raise ExportConfigurationError("publication output directory cannot be prepared") from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_generation_directories(stage: Path, sessions: list[dict]) -> None:
    directories = {stage}
    for session in sessions:
        parent = (stage / session["timeline_path"]).parent
        while parent != stage:
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        _fsync_directory(directory)


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _hash_file(path: Path, byte_limit: int) -> Optional[tuple[int, str]]:
    digest = hashlib.sha256()
    total = 0
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > byte_limit:
            return None
        while True:
            chunk = os.read(fd, min(64 * 1024, byte_limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > byte_limit:
                return None
            digest.update(chunk)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
        ):
            return None
        return total, digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


def _generation_matches(path: Path, expected_files: dict[str, tuple[int, str]]) -> bool:
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        actual_files = set()
        pending = [path]
        entries_seen = 0
        entry_limit = max(100, len(expected_files) * 2 + 20)
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > entry_limit or entry.is_symlink():
                        return False
                    item = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(item)
                    elif entry.is_file(follow_symlinks=False):
                        actual_files.add(item.relative_to(path).as_posix())
                        if len(actual_files) > len(expected_files):
                            return False
                    else:
                        return False
        if actual_files != set(expected_files):
            return False
        for relative, expected in expected_files.items():
            if _hash_file(path / relative, expected[0]) != expected:
                return False
        return True
    except OSError:
        return False


def _atomic_activate(output: Path, pointer_payload: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".current-", dir=output)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(pointer_payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, output / "current.json")
        _fsync_directory(output)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _build_index(
    sessions: list[dict],
    fields: tuple[str, ...],
    allow_capture_gaps: bool,
    redistribution_status: str,
) -> tuple[str, dict, bytes]:
    sessions = sorted(sessions, key=lambda item: (item["symbol"], item["date"]))
    base = {
        "format_version": FORMAT_VERSION,
        "archive_schema_version": 2,
        "result_fields": list(fields),
        "redistribution_status": redistribution_status,
        "integrity_status": "verified_with_gaps" if any(s["integrity_status"] != "verified" for s in sessions) else "verified",
        "capture_gaps_allowed": bool(allow_capture_gaps),
        "sessions": sessions,
    }
    generation_id = _sha256(_canonical_json(base))
    index = dict(base)
    index["generation_id"] = generation_id
    index_payload = _canonical_json(index) + b"\n"
    return generation_id, index, index_payload


def _report_blocked(report: dict, reason: str) -> dict:
    return {
        "status": "blocked",
        "reason": reason,
        "valid_captures": report.get("valid_captures", 0),
        "archive_bytes": report.get("archive_bytes", 0),
        "issue_counts": report.get("issue_counts", {}),
    }


def _redistribution_status(report: dict, policy: Optional[dict]) -> str:
    status = report.get("publisher_readiness", {}).get("redistribution")
    if policy is not None:
        return "operator_attested_not_legal_clearance"
    if status == "synthetic_claim_unverified":
        return "synthetic_claim_unverified"
    return "review_required"


def export_archive(
    archive_dir: Optional[os.PathLike | str],
    output_dir: os.PathLike | str,
    *,
    symbol: Optional[str] = None,
    cadence_seconds: int = 60,
    calendar_file: Optional[os.PathLike | str] = None,
    required_mount: Optional[os.PathLike | str] = None,
    rights_policy: Optional[os.PathLike | str] = None,
    allow_capture_gaps: bool = False,
    allow_synthetic_fixtures: bool = False,
    dry_run: bool = False,
    max_files: int = 10_000,
    max_records: int = 2_000_000,
    max_file_bytes: int = 256 * 1024 * 1024,
    max_line_bytes: int = 16 * 1024 * 1024,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> dict:
    """Verify source JSONL and publish a complete local artifact generation."""
    source = _absolute(archive_dir if archive_dir is not None else archive_root())
    output = _absolute(output_dir)
    if _paths_overlap(source, output):
        raise ExportConfigurationError("archive and output directories must not overlap")
    try:
        policy = _read_rights_policy(rights_policy)
        fields = policy["result_fields"] if policy else DEFAULT_RESULT_FIELDS
        _check_output_layout(output, create=False)
    except ArchiveError as exc:
        raise ExportConfigurationError(str(exc)) from exc

    preview = _TimelineCollector(
        fields,
        allow_non_synthetic=policy is not None,
        allow_synthetic_fixtures=allow_synthetic_fixtures,
    )
    try:
        first_report = verify_archive(
            source,
            symbol=symbol,
            cadence_seconds=cadence_seconds,
            calendar_file=calendar_file,
            required_mount=required_mount,
            max_files=max_files,
            max_records=max_records,
            max_file_bytes=max_file_bytes,
            max_line_bytes=max_line_bytes,
            max_total_bytes=max_total_bytes,
            on_record=preview.add,
        )
    except ArchiveError as exc:
        raise ExportConfigurationError(str(exc)) from exc
    except ExportBlocked:
        raise
    blocked = _readiness_issues(first_report, allow_capture_gaps)
    if blocked:
        return _report_blocked(first_report, "archive integrity is incomplete or invalid")
    if first_report["publisher_readiness"]["redistribution"] == "review_required" and policy is None:
        raise ExportBlocked(
            "non-synthetic archive requires a local operator attestation; "
            "this gate does not establish legal redistribution rights"
        )
    if (
        first_report["publisher_readiness"]["redistribution"] == "synthetic_claim_unverified"
        and policy is None and not allow_synthetic_fixtures
    ):
        raise ExportBlocked("synthetic markers are unauthenticated; explicitly opt in only for known synthetic fixtures")
    first_sessions = preview.session_metadata(first_report)
    first_redistribution_status = _redistribution_status(first_report, policy)
    first_generation_id, first_index, first_index_payload = _build_index(
        first_sessions, fields, allow_capture_gaps, first_redistribution_status,
    )
    if dry_run:
        return {
            "status": "ready",
            "dry_run": True,
            "generation_id": first_generation_id,
            "session_count": len(first_sessions),
            "capture_count": first_report["valid_captures"],
            "archive_bytes": first_report["archive_bytes"],
            "integrity_status": first_index["integrity_status"],
            "redistribution_status": first_redistribution_status,
            "result_fields": list(fields),
        }

    try:
        _check_output_layout(output, create=True)
    except ArchiveError as exc:
        raise ExportConfigurationError(str(exc)) from exc
    staging_root = output / "generations" / ".staging"
    try:
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=staging_root))
    except OSError as exc:
        raise ExportConfigurationError("publication staging directory cannot be created") from exc
    collector = _TimelineCollector(
        fields,
        stage,
        allow_non_synthetic=policy is not None,
        allow_synthetic_fixtures=allow_synthetic_fixtures,
    )
    generation_path = output / "generations" / first_generation_id
    reused_generation = False
    try:
        try:
            second_report = verify_archive(
                source,
                symbol=symbol,
                cadence_seconds=cadence_seconds,
                calendar_file=calendar_file,
                required_mount=required_mount,
                max_files=max_files,
                max_records=max_records,
                max_file_bytes=max_file_bytes,
                max_line_bytes=max_line_bytes,
                max_total_bytes=max_total_bytes,
                on_record=collector.add,
            )
        except ArchiveError as exc:
            raise ExportConfigurationError(str(exc)) from exc
        collector.close()
        if _snapshot_signature(first_report) != _snapshot_signature(second_report):
            raise ExportBlocked("archive changed between verification and export; rerun against a stable snapshot")
        blocked = _readiness_issues(second_report, allow_capture_gaps)
        if blocked:
            return _report_blocked(second_report, "archive changed or became incomplete during export")
        if second_report["publisher_readiness"]["redistribution"] == "review_required" and policy is None:
            raise ExportBlocked(
                "non-synthetic archive requires a local operator attestation; "
                "this gate does not establish legal redistribution rights"
            )
        if (
            second_report["publisher_readiness"]["redistribution"] == "synthetic_claim_unverified"
            and policy is None and not allow_synthetic_fixtures
        ):
            raise ExportBlocked("synthetic markers are unauthenticated; explicitly opt in only for known synthetic fixtures")
        second_sessions = collector.session_metadata(second_report)
        second_redistribution_status = _redistribution_status(second_report, policy)
        generation_id, index, index_payload = _build_index(
            second_sessions, fields, allow_capture_gaps, second_redistribution_status,
        )
        if generation_id != first_generation_id or index_payload != first_index_payload:
            raise ExportBlocked("archive projection changed between verification and export")

        expected_files = {}
        for session in second_sessions:
            expected_files[session["timeline_path"]] = (session["timeline_bytes"], session["timeline_sha256"])
        _write_bytes(stage / "index.json", index_payload)
        expected_files["index.json"] = (len(index_payload), _sha256(index_payload))
        _fsync_generation_directories(stage, second_sessions)
        if not _generation_matches(stage, expected_files):
            raise ExportError("staged artifact generation failed its integrity check")
        generations = output / "generations"
        if generation_path.exists():
            if not _generation_matches(generation_path, expected_files):
                raise ExportConfigurationError("existing generation does not match its content digest")
            shutil.rmtree(stage)
            stage = None
            reused_generation = True
        else:
            try:
                os.replace(stage, generation_path)
            except OSError:
                if not generation_path.exists() or not _generation_matches(generation_path, expected_files):
                    raise
                shutil.rmtree(stage)
                reused_generation = True
            stage = None
        _fsync_directory(generations)
        pointer = {
            "generation_id": generation_id,
            "index_path": f"generations/{generation_id}/index.json",
            "index_sha256": _sha256(index_payload),
        }
        _atomic_activate(output, _canonical_json(pointer) + b"\n")
        return {
            "status": "published",
            "dry_run": False,
            "generation_id": generation_id,
            "session_count": len(second_sessions),
            "capture_count": second_report["valid_captures"],
            "archive_bytes": second_report["archive_bytes"],
            "integrity_status": index["integrity_status"],
            "redistribution_status": second_redistribution_status,
            "result_fields": list(fields),
            "reused_generation": reused_generation,
        }
    except OSError as exc:
        raise ExportConfigurationError("local publication output could not be written safely") from exc
    finally:
        if collector is not None:
            try:
                collector.close()
            except OSError:
                pass
        if stage is not None and stage.exists():
            try:
                shutil.rmtree(stage)
            except OSError:
                pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build local, deterministic historical GEX artifacts")
    parser.add_argument("--archive-dir", help="verified schema-v2 archive root; defaults to FOXCHASE_GEX_DATA_DIR")
    parser.add_argument("--output-dir", required=True, help="local publication output directory")
    parser.add_argument("--symbol", choices=("SPX", "NDX"), help="export one symbol")
    parser.add_argument("--cadence-seconds", type=int, default=60)
    parser.add_argument("--calendar", dest="calendar_file", help="optional calendar JSON for holidays and early closes")
    parser.add_argument("--required-mount", help="require archive to be beneath this mounted directory")
    parser.add_argument(
        "--rights-policy",
        help="local operator attestation only; does not establish legal redistribution rights",
    )
    parser.add_argument("--allow-capture-gaps", action="store_true", help="allow explicitly reported cadence gaps without filling them")
    parser.add_argument(
        "--allow-synthetic-fixtures",
        action="store_true",
        help="explicitly trust unauthenticated synthetic markers for known fabricated fixtures only",
    )
    parser.add_argument("--dry-run", action="store_true", help="verify and plan artifacts without writing")
    parser.add_argument("--max-files", type=int, default=10_000)
    parser.add_argument("--max-records", type=int, default=2_000_000)
    parser.add_argument("--max-file-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-line-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        report = export_archive(
            args.archive_dir,
            args.output_dir,
            symbol=args.symbol,
            cadence_seconds=args.cadence_seconds,
            calendar_file=args.calendar_file,
            required_mount=args.required_mount,
            rights_policy=args.rights_policy,
            allow_capture_gaps=args.allow_capture_gaps,
            allow_synthetic_fixtures=args.allow_synthetic_fixtures,
            dry_run=args.dry_run,
            max_files=args.max_files,
            max_records=args.max_records,
            max_file_bytes=args.max_file_bytes,
            max_line_bytes=args.max_line_bytes,
            max_total_bytes=args.max_total_bytes,
        )
        exit_code = 0 if report["status"] in {"ready", "published"} else 1
    except ExportBlocked as exc:
        report = {"status": "blocked", "reason": str(exc)}
        exit_code = 1
    except (ExportConfigurationError, ExportError) as exc:
        report = {"status": "error", "reason": str(exc)}
        exit_code = 2
    if args.format == "json":
        print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        for key, value in report.items():
            print(f"{key.replace('_', ' ').capitalize()}: {value}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
