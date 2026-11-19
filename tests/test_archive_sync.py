"""Synthetic tests for source-preserving backup archive synchronization."""

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

pytest.importorskip("fcntl")

from gex_client import archive_sync
from gex_client.archive_sync import ArchiveSyncError, _record_sync_failure, export_bundle, import_bundle


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def frame(symbol, day, index, minute):
    stamp = f"{day}T09:{30 + minute:02d}:00-05:00"
    causal = {"symbol": symbol, "synthetic_fixture": True}
    record = {
        "schema_version": 2,
        "captured_at": stamp,
        "request_timestamp": stamp,
        "response_timestamp": stamp,
        "symbol": symbol,
        "frame_index": index,
        "causal_input": causal,
        "provenance": {"provider": "synthetic_fixture", "data_class": "synthetic",
                       "input_content_hash": canonical_hash(causal)},
        "result": {"display_symbol": symbol, "spot": 1.0, "strikes": []},
    }
    record["frame_content_hash"] = canonical_hash(record)
    return record


def write_source(root, symbol="SPX", day="2026-11-16", records=None):
    path = root / symbol / f"{day}.jsonl"
    path.parent.mkdir(parents=True)
    records = records or [frame(symbol, day, 0, 0), frame(symbol, day, 1, 1)]
    path.write_text("".join(json.dumps(item, separators=(",", ":")) + "\n" for item in records))
    return path


def bundle(source, known=None):
    target = io.BytesIO()
    result = export_bundle(source, known or {}, target)
    target.seek(0)
    return target, result


def test_export_import_is_source_scoped_and_idempotent(tmp_path):
    source = tmp_path / "source"
    write_source(source, "SPX")
    write_source(source, "NDX")
    archive = tmp_path / "primary" / "archive"
    mirror = tmp_path / "primary" / "mirror"

    stream, report = bundle(source)
    assert report["files_sent"] == 2
    imported = import_bundle(stream, mirror, archive)
    assert imported["status"] == "imported"
    assert imported["files_added"] == 2
    assert imported["backup_only_captures"] == 4
    assert not archive.exists()
    assert len(list((mirror / "snapshots").glob("*/*/*.jsonl"))) == 2

    stream, report = bundle(source, imported["known"])
    assert report["files_sent"] == 0
    duplicate = import_bundle(stream, mirror, archive)
    assert duplicate["status"] == "unchanged"
    assert duplicate["files_added"] == 0


def test_append_only_source_update_keeps_prior_snapshot(tmp_path):
    source = tmp_path / "source"
    path = write_source(source)
    mirror = tmp_path / "mirror"
    first, _ = bundle(source)
    initial = import_bundle(first, mirror)
    old_sha = initial["known"]["SPX/2026-11-16.jsonl"]["sha256"]

    with path.open("a") as stream:
        stream.write(json.dumps(frame("SPX", "2026-11-16", 2, 2), separators=(",", ":")) + "\n")
    delta, _ = bundle(source, initial["known"])
    updated = import_bundle(delta, mirror)

    assert updated["status"] == "imported"
    assert updated["files_added"] == 1
    versions = list((mirror / "snapshots" / "SPX" / "2026-11-16").glob("*.jsonl"))
    assert len(versions) == 2
    assert any(old_sha in item.name for item in versions)


def test_non_append_source_replacement_is_rejected_and_preserved(tmp_path):
    source = tmp_path / "source"
    path = write_source(source)
    mirror = tmp_path / "mirror"
    first, _ = bundle(source)
    initial = import_bundle(first, mirror)
    prior = list((mirror / "snapshots").glob("*/*/*.jsonl"))[0].read_bytes()

    path.write_text(json.dumps(frame("SPX", "2026-11-16", 0, 0)) + "\n")
    changed, _ = bundle(source, initial["known"])
    result = import_bundle(changed, mirror)

    assert result["status"] == "conflict"
    assert result["conflict_count"] == 1
    assert list((mirror / "snapshots").glob("*/*/*.jsonl"))[0].read_bytes() == prior


def test_overlapping_timestamp_with_different_hash_is_reported_not_merged(tmp_path):
    source = tmp_path / "source"
    backup_record = frame("SPX", "2026-11-16", 0, 0)
    write_source(source, records=[backup_record])
    primary = tmp_path / "primary"
    altered = frame("SPX", "2026-11-16", 0, 0)
    altered["result"]["spot"] = 2.0
    altered.pop("frame_content_hash")
    altered["frame_content_hash"] = canonical_hash(altered)
    primary_path = primary / "SPX" / "2026-11-16.jsonl"
    primary_path.parent.mkdir(parents=True)
    primary_path.write_text(json.dumps(altered) + "\n")
    before = primary_path.read_bytes()

    stream, _ = bundle(source)
    result = import_bundle(stream, tmp_path / "mirror", primary)

    assert result["status"] == "conflict"
    assert result["overlap_conflicts"] == 1
    assert primary_path.read_bytes() == before
    assert not list((tmp_path / "mirror" / "snapshots").glob("*/*/*.jsonl"))


def test_matching_primary_legacy_session_is_counted_without_modification(tmp_path):
    source = tmp_path / "source"
    records = [frame("SPX", "2026-11-16", 0, 0)]
    write_source(source, records=records)
    legacy = tmp_path / "primary-sessions" / "2026-11-16" / "raw" / "existing" / "gex"
    legacy.mkdir(parents=True)
    primary_file = legacy / "2026-11-16.jsonl"
    original = (json.dumps(records[0], separators=(",", ":")) + "\n").encode()
    primary_file.write_bytes(original)

    stream, _ = bundle(source)
    result = import_bundle(
        stream, tmp_path / "mirror", primary_legacy_sessions_root=tmp_path / "primary-sessions"
    )

    assert result["status"] == "imported"
    assert result["identical_primary_captures"] == 1
    assert result["backup_only_captures"] == 0
    assert primary_file.read_bytes() == original


def test_source_symlink_is_rejected(tmp_path):
    source = tmp_path / "source"
    outside = write_source(tmp_path / "outside")
    (source / "SPX").mkdir(parents=True)
    (source / "SPX" / "2026-11-16.jsonl").symlink_to(outside)

    with pytest.raises(ArchiveSyncError, match="SOURCE_OPEN"):
        bundle(source)


def test_required_mount_failure_writes_nothing(tmp_path):
    source = tmp_path / "source"
    write_source(source)
    target = tmp_path / "mirror"
    stream, _ = bundle(source)

    with pytest.raises(ArchiveSyncError, match="REQUIRED_MOUNT_MISSING"):
        import_bundle(stream, target, required_mount=tmp_path, expected_mount_uuid="not-a-uuid")
    assert not target.exists()


def test_required_mount_accepts_same_uuid_systemd_bind_inside_mount(monkeypatch, tmp_path):
    mount = tmp_path / "t7"
    bind_root = mount / "foxchase-gex-backup-mirror"
    mirror = bind_root / "raspberrypi-backup"
    mirror.mkdir(parents=True)
    uuid = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(Path, "is_mount", lambda self: self == mount)

    def findmnt(argv, **kwargs):
        queried = Path(argv[-1])
        target = mount if queried == mount else bind_root
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"{target} {uuid}\n", stderr=""
        )

    monkeypatch.setattr(archive_sync.subprocess, "run", findmnt)

    archive_sync._verify_required_mount(mount, uuid, mirror)


def test_required_mount_rejects_nested_bind_outside_mirror_path(monkeypatch, tmp_path):
    mount = tmp_path / "t7"
    bind_root = mount / "foxchase-gex-backup-mirror"
    mirror = bind_root / "raspberrypi-backup"
    mirror.mkdir(parents=True)
    uuid = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(Path, "is_mount", lambda self: self == mount)

    def findmnt(argv, **kwargs):
        queried = Path(argv[-1])
        target = mount if queried == mount else mount / "different-binding"
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"{target} {uuid}\n", stderr=""
        )

    monkeypatch.setattr(archive_sync.subprocess, "run", findmnt)

    with pytest.raises(ArchiveSyncError, match="REQUIRED_MOUNT_IDENTITY"):
        archive_sync._verify_required_mount(mount, uuid, mirror)


def test_required_mount_rejects_wrong_uuid_on_systemd_bind(monkeypatch, tmp_path):
    mount = tmp_path / "t7"
    bind_root = mount / "foxchase-gex-backup-mirror"
    mirror = bind_root / "raspberrypi-backup"
    mirror.mkdir(parents=True)
    uuid = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(Path, "is_mount", lambda self: self == mount)

    def findmnt(argv, **kwargs):
        queried = Path(argv[-1])
        target = mount if queried == mount else bind_root
        found_uuid = uuid if queried == mount else "wrong-volume-uuid"
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"{target} {found_uuid}\n", stderr=""
        )

    monkeypatch.setattr(archive_sync.subprocess, "run", findmnt)

    with pytest.raises(ArchiveSyncError, match="REQUIRED_MOUNT_IDENTITY"):
        archive_sync._verify_required_mount(mount, uuid, mirror)


def test_manifest_hash_mismatch_is_rejected_without_snapshot(tmp_path):
    source = tmp_path / "source"
    write_source(source)
    stream, _ = bundle(source)
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:*") as archive:
        members = [(member.name, archive.extractfile(member).read()) for member in archive]
    manifest = json.loads(members[0][1])
    manifest["files"][0]["sha256"] = "0" * 64
    members[0] = ("manifest.json", json.dumps(manifest).encode())
    tampered = io.BytesIO()
    with tarfile.open(fileobj=tampered, mode="w") as archive:
        for name, payload in members:
            item = tarfile.TarInfo(name)
            item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))
    tampered.seek(0)

    with pytest.raises(ArchiveSyncError, match="BUNDLE_MANIFEST_MISMATCH"):
        import_bundle(tampered, tmp_path / "mirror")
    assert not list((tmp_path / "mirror" / "snapshots").glob("*/*/*.jsonl"))


def test_corrupt_mirror_index_cannot_select_outside_snapshot_path(tmp_path):
    source = tmp_path / "source"
    write_source(source)
    stream, _ = bundle(source)
    mirror = tmp_path / "mirror"
    mirror.mkdir(mode=0o700)
    index = mirror / "index.json"
    index.write_text(json.dumps({
        "version": 1,
        "sessions": {"SPX/2026-11-16.jsonl": {
            "sha256": "a" * 64,
            "size": 1,
            "snapshot_path": "../../etc/passwd",
        }},
    }))
    index.chmod(0o600)

    with pytest.raises(ArchiveSyncError, match="INDEX_INVALID"):
        import_bundle(stream, mirror)
    assert not list((mirror / "snapshots").glob("*/*/*.jsonl"))


def test_failures_are_durably_journaled_without_arbitrary_error_text(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir(mode=0o700)

    _record_sync_failure(mirror, "REMOTE_TIMEOUT")
    _record_sync_failure(mirror, "REMOTE_TIMEOUT")

    status = json.loads((mirror / "status.json").read_text())
    journal = [json.loads(line) for line in (mirror / "sync_journal.jsonl").read_text().splitlines()]
    assert status["failure_code"] == "REMOTE_TIMEOUT"
    assert status["consecutive_failures"] == 2
    assert [item["status"] for item in journal] == ["failed", "failed"]
    assert all(set(item) <= {"time", "status", "failure_code", "consecutive_failures"} for item in journal)


def test_export_fails_closed_when_source_storage_is_low(tmp_path, monkeypatch):
    source = tmp_path / "source"
    write_source(source)
    monkeypatch.setattr(archive_sync.shutil, "disk_usage", lambda _path: type("Usage", (), {"free": 0})())
    output = io.BytesIO()

    with pytest.raises(ArchiveSyncError, match="SOURCE_LOW_SPACE"):
        export_bundle(source, {}, output)
    assert output.getvalue() == b""
