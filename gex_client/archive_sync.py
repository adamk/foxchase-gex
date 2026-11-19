"""Integrity-checked, source-preserving synchronization of GEX JSONL archives.

The exporter is intended to run as an SSH forced command on the backup host.
The importer runs on the primary and writes only to a source-scoped mirror;
it never edits the primary collector's canonical archive.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO, Optional


PROTOCOL = "foxchase-gex-archive-sync-v1"
SOURCE_ID = "raspberrypi-backup"
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_SESSIONS = 10_000
MIN_FREE_BYTES = 1024 * 1024 * 1024
SESSION_NAME = re.compile(r"^(SPX|NDX)/(\d{4}-\d{2}-\d{2})\.jsonl$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REMOTE_FIXED_CODES = {
    "SOURCE_MOUNT_PIN_REQUIRED", "REQUIRED_MOUNT_MISSING", "REQUIRED_MOUNT_IDENTITY",
    "MIRROR_OUTSIDE_REQUIRED_MOUNT", "SOURCE_LOW_SPACE", "SOURCE_DIRECTORY_UNSAFE",
    "SOURCE_MEMBER_NAME", "SOURCE_OPEN", "SOURCE_TYPE_OR_SIZE", "SOURCE_SHORT_READ",
    "SOURCE_UNSTABLE", "SESSION_PATH_OR_SIZE", "SESSION_DATE", "SESSION_TERMINATION",
    "SESSION_JSON", "SESSION_RECORD", "SESSION_SCHEMA_OR_SYMBOL", "SESSION_INDEX",
    "SESSION_TIMESTAMP", "SESSION_RESULT", "SESSION_RESULT_SYMBOL", "SESSION_PROVENANCE",
    "SESSION_INPUT_SYMBOL", "FRAME_HASH", "INPUT_HASH", "SESSION_EMPTY",
    "SESSION_ORDER_OR_DUPLICATE", "BUNDLE_TOO_LARGE", "REQUEST_FORMAT", "REQUEST_PROTOCOL",
}


class ArchiveSyncError(RuntimeError):
    """A fixed, safe-to-log synchronization failure."""


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_directory(path: Path, *, create: bool = False) -> Path:
    absolute = Path(os.path.abspath(path))
    for component in reversed((absolute, *absolute.parents)):
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            if create:
                component.mkdir(mode=0o700)
                info = os.lstat(component)
            else:
                continue
        except OSError as exc:
            raise ArchiveSyncError("PATH_UNAVAILABLE") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArchiveSyncError("PATH_UNSAFE")
    return absolute


def _private_directory(path: Path, *, create: bool = False) -> Path:
    absolute = _safe_directory(path, create=create)
    info = os.lstat(absolute)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ArchiveSyncError("MIRROR_DIRECTORY_PERMISSIONS")
    return absolute


def _validate_session_bytes(relative: str, payload: bytes) -> dict:
    match = SESSION_NAME.fullmatch(relative)
    if not match or len(payload) > MAX_FILE_BYTES:
        raise ArchiveSyncError("SESSION_PATH_OR_SIZE")
    symbol, day_text = match.groups()
    try:
        day = date.fromisoformat(day_text).isoformat()
    except ValueError as exc:
        raise ArchiveSyncError("SESSION_DATE") from exc
    if day != day_text or not payload.endswith(b"\n"):
        raise ArchiveSyncError("SESSION_TERMINATION")

    records = []
    parsed_stamps = []
    for line in payload.splitlines():
        try:
            record = json.loads(line, object_pairs_hook=_unique_keys,
                                parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ArchiveSyncError("SESSION_JSON") from exc
        if not isinstance(record, dict):
            raise ArchiveSyncError("SESSION_RECORD")
        if record.get("schema_version") != 2 or record.get("symbol") != symbol:
            raise ArchiveSyncError("SESSION_SCHEMA_OR_SYMBOL")
        if isinstance(record.get("frame_index"), bool) or not isinstance(record.get("frame_index"), int):
            raise ArchiveSyncError("SESSION_INDEX")
        stamp = record.get("captured_at")
        if not isinstance(stamp, str):
            raise ArchiveSyncError("SESSION_TIMESTAMP")
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ArchiveSyncError("SESSION_TIMESTAMP") from exc
        if parsed.tzinfo is None or parsed.date().isoformat() != day:
            raise ArchiveSyncError("SESSION_TIMESTAMP")
        result = record.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("strikes"), list):
            raise ArchiveSyncError("SESSION_RESULT")
        if result.get("display_symbol") is not None and str(result["display_symbol"]).upper() != symbol:
            raise ArchiveSyncError("SESSION_RESULT_SYMBOL")
        causal = record.get("causal_input")
        provenance = record.get("provenance")
        if not isinstance(causal, dict) or not isinstance(provenance, dict):
            raise ArchiveSyncError("SESSION_PROVENANCE")
        causal_symbol = causal.get("symbol")
        if causal_symbol is not None and str(causal_symbol).upper() not in {symbol, f"${symbol}"}:
            raise ArchiveSyncError("SESSION_INPUT_SYMBOL")
        digest = record.get("frame_content_hash")
        body = dict(record)
        body.pop("frame_content_hash", None)
        try:
            valid_hash = isinstance(digest, str) and bool(SHA256.fullmatch(digest)) and _canonical_hash(body) == digest
        except (TypeError, ValueError):
            valid_hash = False
        if not valid_hash:
            raise ArchiveSyncError("FRAME_HASH")
        if causal:
            try:
                input_hash = _canonical_hash(causal)
            except (TypeError, ValueError):
                raise ArchiveSyncError("INPUT_HASH")
            if provenance.get("input_content_hash") != input_hash:
                raise ArchiveSyncError("INPUT_HASH")
        parsed_stamps.append(parsed)
        records.append(record)
    if not records:
        raise ArchiveSyncError("SESSION_EMPTY")

    captures = {}
    for index, record in enumerate(records):
        stamp = record["captured_at"]
        if record["frame_index"] != index or stamp in captures:
            raise ArchiveSyncError("SESSION_ORDER_OR_DUPLICATE")
        if index and parsed_stamps[index] <= parsed_stamps[index - 1]:
            raise ArchiveSyncError("SESSION_ORDER_OR_DUPLICATE")
        captures[stamp] = record["frame_content_hash"]
    return {
        "symbol": symbol,
        "date": day,
        "sha256": _file_hash(payload),
        "size": len(payload),
        "capture_count": len(records),
        "first_captured_at": records[0]["captured_at"],
        "last_captured_at": records[-1]["captured_at"],
        "captures": captures,
    }


def _unique_keys(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate key")
        output[key] = value
    return output


def _reject_constant(value):
    raise ValueError("non-finite value")


def _read_stable_file(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ArchiveSyncError("SOURCE_OPEN") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE_BYTES:
            raise ArchiveSyncError("SOURCE_TYPE_OR_SIZE")
        pieces = []
        remaining = before.st_size
        while remaining:
            part = os.read(fd, min(1024 * 1024, remaining))
            if not part:
                raise ArchiveSyncError("SOURCE_SHORT_READ")
            pieces.append(part)
            remaining -= len(part)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ArchiveSyncError("SOURCE_UNSTABLE")
        return b"".join(pieces)
    finally:
        os.close(fd)


def export_bundle(
    archive_root: Path,
    known: dict,
    output: BinaryIO,
    *,
    required_mount: Optional[Path] = None,
    expected_mount_uuid: Optional[str] = None,
) -> dict:
    """Write changed, validated sessions as a manifest-first tar stream."""
    root = _safe_directory(Path(archive_root))
    _verify_required_mount(required_mount, expected_mount_uuid, root)
    if shutil.disk_usage(root).free < MIN_FREE_BYTES:
        raise ArchiveSyncError("SOURCE_LOW_SPACE")
    if not isinstance(known, dict):
        raise ArchiveSyncError("REQUEST_FORMAT")
    sessions = []
    payloads = {}
    total = 0
    for symbol in ("SPX", "NDX"):
        directory = root / symbol
        try:
            directory_stat = os.lstat(directory)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            raise ArchiveSyncError("SOURCE_DIRECTORY_UNSAFE")
        for path in sorted(directory.iterdir()):
            if not path.name.endswith(".jsonl"):
                continue
            relative = f"{symbol}/{path.name}"
            if not SESSION_NAME.fullmatch(relative):
                raise ArchiveSyncError("SOURCE_MEMBER_NAME")
            payload = _read_stable_file(path)
            info = _validate_session_bytes(relative, payload)
            prior = known.get(relative)
            if prior is not None:
                if not isinstance(prior, dict) or not SHA256.fullmatch(str(prior.get("sha256", ""))):
                    raise ArchiveSyncError("REQUEST_FORMAT")
                if prior["sha256"] == info["sha256"]:
                    continue
            total += len(payload)
            if total > MAX_BUNDLE_BYTES:
                raise ArchiveSyncError("BUNDLE_TOO_LARGE")
            payloads[relative] = payload
            sessions.append({key: value for key, value in info.items() if key != "captures"} | {"path": relative})

    manifest = {"protocol": PROTOCOL, "source": SOURCE_ID, "files": sessions}
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        metadata = tarfile.TarInfo("manifest.json")
        metadata.size = len(manifest_bytes)
        metadata.mode = 0o600
        archive.addfile(metadata, io.BytesIO(manifest_bytes))
        for relative, payload in payloads.items():
            member = tarfile.TarInfo("files/" + relative)
            member.size = len(payload)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(payload))
    return {"files_sent": len(sessions), "bytes_sent": total}


def _read_index(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {"version": 1, "sessions": {}}
    except OSError as exc:
        raise ArchiveSyncError("INDEX_PATH_UNSAFE") from exc
    owns_fd = True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024:
            raise ArchiveSyncError("INDEX_PATH_UNSAFE")
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ArchiveSyncError("INDEX_PERMISSIONS")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            owns_fd = False
            value = json.load(stream)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ArchiveSyncError("INDEX_INVALID") from exc
    finally:
        if owns_fd:
            os.close(fd)
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("sessions"), dict):
        raise ArchiveSyncError("INDEX_INVALID")
    for relative, entry in value["sessions"].items():
        match = SESSION_NAME.fullmatch(relative) if isinstance(relative, str) else None
        if not match or not isinstance(entry, dict):
            raise ArchiveSyncError("INDEX_INVALID")
        digest = entry.get("sha256")
        symbol, day = match.groups()
        expected_snapshot = f"snapshots/{symbol}/{day}/{digest}.jsonl"
        if (not isinstance(digest, str) or not SHA256.fullmatch(digest)
                or entry.get("snapshot_path") != expected_snapshot
                or not isinstance(entry.get("size"), int) or entry["size"] < 0):
            raise ArchiveSyncError("INDEX_INVALID")
    return value


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict) -> None:
    try:
        old = os.lstat(path)
    except FileNotFoundError:
        old = None
    if old is not None and (
        not stat.S_ISREG(old.st_mode) or old.st_nlink != 1
        or old.st_uid != os.geteuid() or old.st_mode & 0o077
    ):
        raise ArchiveSyncError("METADATA_PATH_UNSAFE")
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _append_journal(path: Path, event: dict) -> None:
    safe = {key: event[key] for key in (
        "time", "status", "files_added", "files_unchanged", "conflict_count",
        "overlap_conflicts", "backup_only_captures", "primary_only_captures",
        "identical_primary_captures", "failure_code", "consecutive_failures",
    ) if key in event}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ArchiveSyncError("JOURNAL_PATH_UNSAFE")
        os.write(fd, (json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _install_snapshot(root: Path, relative: str, digest: str, payload: bytes) -> Path:
    match = SESSION_NAME.fullmatch(relative)
    if not match or _file_hash(payload) != digest:
        raise ArchiveSyncError("SNAPSHOT_HASH")
    symbol, day = match.groups()
    parent = root / "snapshots" / symbol / day
    _private_directory(parent, create=True)
    destination = parent / f"{digest}.jsonl"
    try:
        existing = os.open(destination, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        existing = None
    if existing is not None:
        try:
            found = hashlib.sha256()
            while True:
                part = os.read(existing, 1024 * 1024)
                if not part:
                    break
                found.update(part)
            existing_info = os.fstat(existing)
            if (not stat.S_ISREG(existing_info.st_mode) or existing_info.st_nlink != 1
                    or existing_info.st_uid != os.geteuid() or existing_info.st_mode & 0o077
                    or found.hexdigest() != digest):
                raise ArchiveSyncError("SNAPSHOT_COLLISION")
        finally:
            os.close(existing)
        return destination

    stage_dir = root / ".staging"
    _private_directory(stage_dir, create=True)
    fd, temp_name = tempfile.mkstemp(prefix="snapshot-", dir=stage_dir)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, destination, follow_symlinks=False)
        except FileExistsError:
            return _install_snapshot(root, relative, digest, payload)
        _fsync_dir(parent)
        return destination
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _primary_overlap(
    path: Path,
    relative: str,
    backup_capture_hashes: dict,
    *,
    allow_other_symbol: bool = False,
) -> Optional[dict]:
    try:
        payload = _read_stable_file(path)
    except FileNotFoundError:
        return None
    except ArchiveSyncError:
        return {"identical": 0, "conflicts": 1, "backup_only": 0, "primary_only": 0}
    records = []
    try:
        for line in payload.splitlines():
            records.append(json.loads(line))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"identical": 0, "conflicts": 1, "backup_only": 0, "primary_only": 0}
    symbol = relative.split("/", 1)[0]
    source_symbols = {item.get("symbol") for item in records if isinstance(item, dict)}
    if allow_other_symbol and source_symbols and source_symbols == ({"SPX", "NDX"} - {symbol}):
        return None
    try:
        _validate_session_bytes(relative, payload)
    except ArchiveSyncError:
        return {"identical": 0, "conflicts": 1, "backup_only": 0, "primary_only": 0}
    primary_captures = {record["captured_at"]: record["frame_content_hash"] for record in records}
    identical = 0
    conflicts = 0
    backup_only = 0
    for stamp, primary_hash in primary_captures.items():
        mirror_hash = backup_capture_hashes.get(stamp)
        if mirror_hash is None:
            continue
        if mirror_hash == primary_hash:
            identical += 1
        else:
            conflicts += 1
    backup_only = sum(1 for stamp in backup_capture_hashes if stamp not in primary_captures)
    primary_only = sum(1 for stamp in primary_captures if stamp not in backup_capture_hashes)
    return {"identical": identical, "conflicts": conflicts,
            "backup_only": backup_only, "primary_only": primary_only}


def _verify_required_mount(required_mount: Optional[Path], expected_uuid: Optional[str], mirror: Path) -> None:
    if required_mount is None:
        return
    mount = Path(os.path.abspath(required_mount))
    mirror_absolute = Path(os.path.abspath(mirror))
    try:
        mirror_absolute.relative_to(mount)
    except ValueError as exc:
        raise ArchiveSyncError("MIRROR_OUTSIDE_REQUIRED_MOUNT") from exc
    if not mount.is_mount():
        raise ArchiveSyncError("REQUIRED_MOUNT_MISSING")
    if expected_uuid:
        for path in (mount, mirror_absolute):
            result = subprocess.run(
                ["findmnt", "--noheadings", "--output", "TARGET,UUID", "--target", str(path)],
                capture_output=True, text=True, timeout=5, check=False,
            )
            fields = result.stdout.strip().split()
            if result.returncode or len(fields) != 2 or fields[1] != expected_uuid:
                raise ArchiveSyncError("REQUIRED_MOUNT_IDENTITY")
            target = Path(os.path.abspath(fields[0]))
            try:
                target.relative_to(mount)
                path.relative_to(target)
            except ValueError as exc:
                raise ArchiveSyncError("REQUIRED_MOUNT_IDENTITY") from exc
            if path == mount and target != mount:
                raise ArchiveSyncError("REQUIRED_MOUNT_IDENTITY")


def _existing_safe_directory(path: Optional[Path]) -> None:
    if path is not None and Path(os.path.abspath(path)).exists():
        _safe_directory(Path(path))


def import_bundle(
    bundle: BinaryIO,
    mirror_root: Path,
    primary_archive_root: Optional[Path] = None,
    *,
    required_mount: Optional[Path] = None,
    expected_mount_uuid: Optional[str] = None,
    primary_legacy_sessions_root: Optional[Path] = None,
) -> dict:
    """Validate a transfer and store immutable content-addressed snapshots."""
    mirror = Path(os.path.abspath(mirror_root))
    _verify_required_mount(required_mount, expected_mount_uuid, mirror)
    _existing_safe_directory(Path(primary_archive_root) if primary_archive_root else None)
    _existing_safe_directory(Path(primary_legacy_sessions_root) if primary_legacy_sessions_root else None)
    if mirror.exists() and stat.S_ISLNK(os.lstat(mirror).st_mode):
        raise ArchiveSyncError("MIRROR_PATH_UNSAFE")
    _private_directory(mirror, create=True)
    staging = mirror / ".staging"
    _private_directory(staging, create=True)
    lock_path = mirror / "sync.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or lock_info.st_uid != os.geteuid() or lock_info.st_mode & 0o077):
            raise ArchiveSyncError("LOCK_PATH_UNSAFE")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ArchiveSyncError:
        os.close(lock_fd)
        raise
    except OSError as exc:
        os.close(lock_fd)
        raise ArchiveSyncError("SYNC_ALREADY_RUNNING") from exc

    try:
        index_path = mirror / "index.json"
        index = _read_index(index_path)
        staged = {}
        manifest = None
        total_size = 0
        with tarfile.open(fileobj=bundle, mode="r:*") as archive:
            seen = set()
            for member in archive:
                if member.name in seen or not member.isfile() or member.issym() or member.islnk():
                    raise ArchiveSyncError("BUNDLE_MEMBER_UNSAFE")
                seen.add(member.name)
                if member.name == "manifest.json":
                    if manifest is not None or member.size > 16 * 1024 * 1024:
                        raise ArchiveSyncError("BUNDLE_MANIFEST")
                    extracted = archive.extractfile(member)
                    manifest = json.loads(extracted.read().decode("utf-8"))
                    continue
                if manifest is None or not member.name.startswith("files/"):
                    raise ArchiveSyncError("BUNDLE_ORDER")
                relative = member.name[6:]
                if not SESSION_NAME.fullmatch(relative) or member.size > MAX_FILE_BYTES:
                    raise ArchiveSyncError("BUNDLE_PATH_OR_SIZE")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ArchiveSyncError("BUNDLE_MEMBER_READ")
                payload = extracted.read(MAX_FILE_BYTES + 1)
                if len(payload) != member.size or len(payload) > MAX_FILE_BYTES:
                    raise ArchiveSyncError("BUNDLE_MEMBER_READ")
                staged[relative] = payload
                total_size += len(payload)
                if total_size > MAX_BUNDLE_BYTES or len(staged) > MAX_SESSIONS:
                    raise ArchiveSyncError("BUNDLE_TOO_LARGE")

        if (not isinstance(manifest, dict) or manifest.get("protocol") != PROTOCOL
                or manifest.get("source") != SOURCE_ID or not isinstance(manifest.get("files"), list)):
            raise ArchiveSyncError("BUNDLE_MANIFEST")
        expected = {item.get("path"): item for item in manifest["files"] if isinstance(item, dict)}
        if len(expected) != len(manifest["files"]) or set(expected) != set(staged):
            raise ArchiveSyncError("BUNDLE_MEMBERS_MISMATCH")
        if shutil.disk_usage(mirror).free < total_size + MIN_FREE_BYTES:
            raise ArchiveSyncError("MIRROR_LOW_SPACE")

        additions = 0
        unchanged = 0
        conflict_count = 0
        overlap_conflicts = 0
        backup_only_captures = 0
        primary_only_captures = 0
        identical_primary_captures = 0
        sessions = index["sessions"]
        primary_root = Path(primary_archive_root) if primary_archive_root else None
        for relative, payload in sorted(staged.items()):
            info = _validate_session_bytes(relative, payload)
            declared = expected[relative]
            for key in ("sha256", "size", "capture_count", "first_captured_at", "last_captured_at"):
                if declared.get(key) != info[key]:
                    raise ArchiveSyncError("BUNDLE_MANIFEST_MISMATCH")
            previous = sessions.get(relative)
            snapshot_path = None
            if previous is not None:
                old_path = mirror / previous.get("snapshot_path", "")
                try:
                    old_payload = _read_stable_file(old_path)
                except ArchiveSyncError as exc:
                    raise ArchiveSyncError("MIRROR_PREIMAGE_INVALID") from exc
                if _file_hash(old_payload) != previous.get("sha256"):
                    raise ArchiveSyncError("MIRROR_PREIMAGE_HASH")
                if info["sha256"] == previous["sha256"]:
                    unchanged += 1
                    continue
                if not payload.startswith(old_payload):
                    conflict_count += 1
                    continue
            primary_comparison = None
            if primary_root is not None:
                primary_path = primary_root / info["symbol"] / f"{info['date']}.jsonl"
                primary_comparison = _primary_overlap(
                    primary_path, relative, info["captures"]
                )
            if primary_comparison is None and primary_legacy_sessions_root is not None:
                legacy_path = (
                    Path(primary_legacy_sessions_root) / info["date"] / "raw" /
                    "existing" / "gex" / f"{info['date']}.jsonl"
                )
                primary_comparison = _primary_overlap(
                    legacy_path, relative, info["captures"], allow_other_symbol=True
                )
            if primary_comparison is None:
                primary_comparison = {
                    "identical": 0, "conflicts": 0,
                    "backup_only": info["capture_count"], "primary_only": 0,
                }
            identical = primary_comparison["identical"]
            overlap = primary_comparison["conflicts"]
            backup_only_captures += primary_comparison["backup_only"]
            primary_only_captures += primary_comparison["primary_only"]
            identical_primary_captures += identical
            overlap_conflicts += overlap
            if overlap:
                conflict_count += 1
                continue
            snapshot_path = _install_snapshot(mirror, relative, info["sha256"], payload)
            sessions[relative] = {
                "sha256": info["sha256"],
                "size": info["size"],
                "capture_count": info["capture_count"],
                "first_captured_at": info["first_captured_at"],
                "last_captured_at": info["last_captured_at"],
                "identical_primary_captures": identical,
                "backup_only_captures": primary_comparison["backup_only"],
                "primary_only_captures": primary_comparison["primary_only"],
                "snapshot_path": str(snapshot_path.relative_to(mirror)),
            }
            additions += 1

        if additions:
            _atomic_json(index_path, index)
        status = "conflict" if conflict_count else ("imported" if additions else "unchanged")
        event = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": status,
            "files_added": additions,
            "files_unchanged": unchanged,
            "conflict_count": conflict_count,
            "overlap_conflicts": overlap_conflicts,
            "backup_only_captures": backup_only_captures,
            "primary_only_captures": primary_only_captures,
            "identical_primary_captures": identical_primary_captures,
        }
        _append_journal(mirror / "sync_journal.jsonl", event)
        _atomic_json(mirror / "status.json", event)
        known = {key: {"sha256": value["sha256"], "size": value["size"]} for key, value in sessions.items()}
        return {**event, "known": known}
    finally:
        os.close(lock_fd)


def _known_from_mirror(mirror_root: Path) -> dict:
    index = _read_index(Path(mirror_root) / "index.json")
    return {key: {"sha256": value["sha256"], "size": value["size"]}
            for key, value in index["sessions"].items()}


def _record_sync_failure(mirror: Path, failure_code: str) -> None:
    consecutive = 1
    try:
        prior = json.loads((mirror / "status.json").read_text(encoding="utf-8"))
        consecutive = int(prior.get("consecutive_failures", 0)) + 1 if prior.get("status") == "failed" else 1
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        pass
    event = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "failed",
        "failure_code": failure_code,
        "consecutive_failures": consecutive,
    }
    try:
        _append_journal(mirror / "sync_journal.jsonl", event)
        _atomic_json(mirror / "status.json", event)
    except OSError:
        # The service's nonzero exit remains visible in the system journal.
        pass


def sync_once(args) -> dict:
    mirror = Path(args.mirror_root)
    _verify_required_mount(Path(args.required_mount), args.expected_mount_uuid, mirror)
    _safe_directory(mirror, create=True)
    try:
        if shutil.disk_usage(mirror).free < MIN_FREE_BYTES:
            raise ArchiveSyncError("MIRROR_LOW_SPACE")
        known = _known_from_mirror(mirror)
        request = json.dumps({"protocol": PROTOCOL, "known": known}, sort_keys=True, separators=(",", ":")).encode()
        stage = mirror / ".staging"
        _safe_directory(stage, create=True)
        fd, bundle_name = tempfile.mkstemp(prefix="incoming-", dir=stage)
        bundle_path = Path(bundle_name)
        try:
            with os.fdopen(fd, "wb") as output:
                command = [
                    "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "UpdateHostKeys=no", "-o", "IdentitiesOnly=yes",
                    "-o", "ConnectTimeout=5", "-o", f"UserKnownHostsFile={args.known_hosts}",
                    "-o", f"HostKeyAlias={args.host_key_alias}", "-i", args.identity,
                    args.remote,
                ]
                result = subprocess.run(command, input=request, stdout=output,
                                        stderr=subprocess.PIPE, timeout=180, check=False)
                output.flush()
                os.fsync(output.fileno())
            if result.returncode:
                lines = result.stderr.decode("ascii", errors="ignore").splitlines()
                code = next((line.strip() for line in lines if line.strip() in REMOTE_FIXED_CODES),
                            "REMOTE_EXPORT_FAILED")
                raise ArchiveSyncError(code)
            with bundle_path.open("rb") as stream:
                return import_bundle(
                    stream, mirror, Path(args.primary_archive_root),
                    required_mount=Path(args.required_mount), expected_mount_uuid=args.expected_mount_uuid,
                    primary_legacy_sessions_root=Path(args.primary_legacy_sessions_root),
                )
        finally:
            try:
                bundle_path.unlink()
            except FileNotFoundError:
                pass
    except ArchiveSyncError as exc:
        _record_sync_failure(mirror, str(exc))
        raise
    except subprocess.TimeoutExpired as exc:
        _record_sync_failure(mirror, "REMOTE_TIMEOUT")
        raise ArchiveSyncError("REMOTE_TIMEOUT") from exc
    except (OSError, tarfile.TarError, json.JSONDecodeError, UnicodeError) as exc:
        _record_sync_failure(mirror, "SYNC_IO_OR_PROTOCOL_ERROR")
        raise ArchiveSyncError("SYNC_IO_OR_PROTOCOL_ERROR") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize immutable schema-v2 GEX archive snapshots")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--export", action="store_true")
    modes.add_argument("--sync", action="store_true")
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--mirror-root", type=Path)
    parser.add_argument("--primary-archive-root", type=Path)
    parser.add_argument("--primary-legacy-sessions-root", type=Path)
    parser.add_argument("--required-mount", type=Path)
    parser.add_argument("--expected-mount-uuid")
    parser.add_argument("--remote")
    parser.add_argument("--identity")
    parser.add_argument("--known-hosts")
    parser.add_argument("--host-key-alias")
    args = parser.parse_args(argv)
    try:
        if args.export:
            if args.archive_root is None:
                raise ArchiveSyncError("ARCHIVE_ROOT_REQUIRED")
            if args.required_mount is None or not args.expected_mount_uuid:
                raise ArchiveSyncError("SOURCE_MOUNT_PIN_REQUIRED")
            request = json.load(sys.stdin)
            if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
                raise ArchiveSyncError("REQUEST_PROTOCOL")
            report = export_bundle(
                args.archive_root, request.get("known", {}), sys.stdout.buffer,
                required_mount=args.required_mount,
                expected_mount_uuid=args.expected_mount_uuid,
            )
            sys.stderr.write(f"export files={report['files_sent']} bytes={report['bytes_sent']}\n")
            return 0
        required = (args.mirror_root, args.primary_archive_root, args.primary_legacy_sessions_root, args.required_mount,
                    args.remote, args.identity, args.known_hosts, args.host_key_alias,
                    args.expected_mount_uuid)
        if not all(required):
            raise ArchiveSyncError("SYNC_ARGUMENTS_REQUIRED")
        report = sync_once(args)
        print(json.dumps({key: value for key, value in report.items() if key != "known"}, sort_keys=True))
        return 1 if report["status"] == "conflict" else 0
    except ArchiveSyncError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except (OSError, tarfile.TarError, subprocess.TimeoutExpired, json.JSONDecodeError, UnicodeError):
        sys.stderr.write("SYNC_IO_OR_PROTOCOL_ERROR\n")
        return 1
    except (AttributeError, KeyError, TypeError, ValueError):
        sys.stderr.write("SYNC_INPUT_INVALID\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
