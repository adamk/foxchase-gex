"""Synthetic-only tests for the archive verification CLI."""

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def reseal(record):
    record.pop("frame_content_hash", None)
    record["frame_content_hash"] = canonical_hash(record)
    return record


def frame(symbol="SPX", stamp="2026-10-01T09:30:00-04:00", index=0):
    causal_input = {
        "symbol": symbol,
        "spot": 5100.0,
        "synthetic_fixture": True,
        "contracts": [{"option_type": "CALL", "strike": 5100.0, "open_interest": 12}],
    }
    value = {
        "schema_version": 2,
        "captured_at": stamp,
        "request_timestamp": stamp,
        "response_timestamp": stamp,
        "symbol": symbol,
        "frame_index": index,
        "causal_input": causal_input,
        "provenance": {
            "provider": "synthetic_fixture",
            "data_class": "synthetic",
            "input_content_hash": canonical_hash(causal_input),
        },
        "result": {
            "display_symbol": symbol,
            "spot": 5100.0,
            "strikes": [{"strike": 5100.0, "gex": 1.25, "call_gex": 1.5, "put_gex": 0.25}],
            "structure": {"net_gex": 1.25, "gamma_flip": 5090.0, "call_wall": 5120.0, "put_wall": 5070.0},
            "patterns": {"primary": "synthetic-only"},
        },
    }
    value["frame_content_hash"] = canonical_hash(value)
    return value


def write_session(root, symbol="SPX", day="2026-10-01", records=None, trailing_newline=True):
    directory = root / symbol
    directory.mkdir(parents=True, exist_ok=True)
    records = records if records is not None else [
        frame(symbol, f"{day}T09:30:00-04:00", 0),
        frame(symbol, f"{day}T09:31:00-04:00", 1),
    ]
    raw = "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
    if trailing_newline:
        raw += "\n"
    path = directory / f"{day}.jsonl"
    path.write_text(raw, encoding="utf-8")
    return path


def run_verify(root, *args):
    return subprocess.run(
        [sys.executable, "-m", "gex_client.history_verify", str(root), "--format", "json", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def test_verifier_reports_valid_synthetic_sessions_and_health(tmp_path):
    spx_path = write_session(tmp_path, "SPX", "2026-10-01")
    ndx_path = write_session(tmp_path, "NDX", "2026-10-01", [
        frame("NDX", "2026-10-01T09:30:00-04:00", 0),
        frame("NDX", "2026-10-01T09:31:00-04:00", 1),
    ])

    result = run_verify(tmp_path)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "valid"
    assert report["symbols"]["SPX"]["session_count"] == 1
    assert report["symbols"]["NDX"]["latest_successful_capture"] == "2026-10-01T09:31:00-04:00"
    assert report["files_found"] == report["files_scanned"] == 2
    assert report["archive_bytes"] == spx_path.stat().st_size + ndx_path.stat().st_size
    spx_session = report["symbols"]["SPX"]["sessions"][0]
    assert spx_session["start_timestamp"] == "2026-10-01T09:30:00-04:00"
    assert spx_session["end_timestamp"] == "2026-10-01T09:31:00-04:00"

    text_result = subprocess.run(
        [sys.executable, "-m", "gex_client.history_verify", str(tmp_path), "--symbol", "SPX"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert text_result.returncode == 0
    assert "Archive status: valid" in text_result.stdout


def test_verifier_detects_hash_tampering(tmp_path):
    first = frame()
    first["result"]["spot"] = 1.0
    path = write_session(tmp_path, records=[first])
    before = path.read_bytes()

    result = run_verify(tmp_path)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert "frame_hash_mismatch" in {issue["code"] for issue in report["issues"]}
    assert path.read_bytes() == before


def test_verifier_reports_duplicate_indexes_order_and_capture_gaps(tmp_path):
    records = [
        frame(stamp="2026-10-01T09:30:00-04:00", index=0),
        frame(stamp="2026-10-01T09:31:00-04:00", index=0),
        frame(stamp="2026-10-01T09:33:00-04:00", index=3),
        frame(stamp="2026-10-01T09:32:00-04:00", index=2),
        frame(stamp="2026-10-01T09:32:00-04:00", index=5),
    ]
    for record in records:
        reseal(record)
    write_session(tmp_path, records=records)

    result = run_verify(tmp_path)

    assert result.returncode == 1
    codes = {issue["code"] for issue in json.loads(result.stdout)["issues"]}
    assert {
        "duplicate_frame_index", "duplicate_capture", "missing_frame_index",
        "nonmonotonic_timestamp", "nonmonotonic_frame_index", "capture_gap",
    } <= codes


def test_verifier_summarizes_incomplete_sessions_separately(tmp_path):
    write_session(tmp_path, records=[
        frame(stamp="2026-10-01T09:30:00-04:00", index=0),
        frame(stamp="2026-10-01T09:33:00-04:00", index=3),
    ])

    result = run_verify(tmp_path, "--symbol", "SPX")
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["symbols"]["SPX"]["incomplete_sessions"] == ["2026-10-01"]
    assert report["symbols"]["SPX"]["invalid_sessions"] == []


def test_verifier_detects_truncated_tail_without_repairing_it(tmp_path):
    path = write_session(tmp_path, records=[frame()], trailing_newline=False)
    before = path.read_bytes()

    result = run_verify(tmp_path)

    assert result.returncode == 1
    codes = {issue["code"] for issue in json.loads(result.stdout)["issues"]}
    assert "truncated_final_record" in codes
    assert path.read_bytes() == before


def test_verifier_reports_a_malformed_complete_jsonl_line(tmp_path):
    path = write_session(tmp_path, records=[frame()])
    with path.open("ab") as stream:
        stream.write(b"{not-json}\n")

    result = run_verify(tmp_path)

    assert result.returncode == 1
    codes = {issue["code"] for issue in json.loads(result.stdout)["issues"]}
    assert "malformed_json" in codes


def test_verifier_rejects_symbol_date_and_input_hash_mismatch(tmp_path):
    bad = frame(symbol="NDX", stamp="2026-10-02T09:30:00-04:00", index=0)
    bad["provenance"]["input_content_hash"] = "0" * 64
    reseal(bad)
    write_session(tmp_path, "SPX", "2026-10-01", [bad])

    result = run_verify(tmp_path)

    assert result.returncode == 1
    codes = {issue["code"] for issue in json.loads(result.stdout)["issues"]}
    assert {"symbol_mismatch", "session_date_mismatch", "input_hash_mismatch"} <= codes


def test_verifier_reports_symlink_and_unexpected_entries_without_following(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.jsonl"
    outside.write_text(json.dumps(frame()) + "\n", encoding="utf-8")
    spx = tmp_path / "SPX"
    spx.mkdir()
    (spx / "2026-10-01.jsonl").symlink_to(outside)
    (spx / "latest.json").write_text("{}\n", encoding="utf-8")
    before = outside.read_bytes()

    result = run_verify(tmp_path)

    assert result.returncode == 1
    codes = {issue["code"] for issue in json.loads(result.stdout)["issues"]}
    assert "symlink_entry" in codes
    assert "unexpected_entry" in codes
    assert outside.read_bytes() == before


def test_verifier_reports_missing_root_and_bad_mount_as_exit_two(tmp_path):
    missing = run_verify(tmp_path / "missing")
    assert missing.returncode == 2
    assert json.loads(missing.stdout)["status"] == "unavailable"

    write_session(tmp_path, records=[frame()])
    bad_mount = run_verify(tmp_path, "--required-mount", str(tmp_path))
    assert bad_mount.returncode == 2
    assert json.loads(bad_mount.stdout)["status"] == "unavailable"


def test_verifier_uses_supplied_calendar_to_identify_absent_sessions(tmp_path):
    write_session(tmp_path, "SPX", "2026-10-02", [frame(stamp="2026-10-02T09:30:00-04:00")])
    write_session(tmp_path, "NDX", "2026-10-02", [frame("NDX", "2026-10-02T09:30:00-04:00")])
    calendar = tmp_path.parent / f"{tmp_path.name}-calendar.json"
    calendar.write_text(json.dumps({
        "holidays": ["2026-10-01"],
        "trading_sessions": ["2026-10-01", "2026-10-02"],
        "early_closes": {"2026-10-02": "13:00"},
    }), encoding="utf-8")

    result = run_verify(tmp_path, "--calendar", str(calendar))

    assert result.returncode == 0
    assert "missing_trading_session" not in {
        issue["code"] for issue in json.loads(result.stdout)["issues"]
    }


def test_verifier_rejects_early_close_outside_configured_session(tmp_path):
    write_session(tmp_path, "SPX", "2026-10-02", [frame(stamp="2026-10-02T09:30:00-04:00")])
    calendar = tmp_path.parent / f"{tmp_path.name}-invalid-calendar.json"
    calendar.write_text(json.dumps({"early_closes": {"2026-10-02": "09:00"}}), encoding="utf-8")

    result = run_verify(tmp_path, "--calendar", str(calendar))

    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "unavailable"


def test_verifier_classifies_weekend_and_early_close_periods(tmp_path):
    weekend = tmp_path / "weekend"
    write_session(weekend, "SPX", "2026-10-03", [
        frame(stamp="2026-10-03T09:30:00-04:00", index=0),
        frame(stamp="2026-10-03T09:33:00-04:00", index=1),
    ])
    weekend_result = run_verify(weekend, "--symbol", "SPX")
    weekend_codes = {issue["code"] for issue in json.loads(weekend_result.stdout)["issues"]}
    assert "capture_during_noncollection_period" in weekend_codes
    assert "capture_gap" not in weekend_codes

    early_close = tmp_path / "early-close"
    write_session(early_close, "SPX", "2026-10-02", [
        frame(stamp="2026-10-02T12:00:00-04:00", index=0),
        frame(stamp="2026-10-02T14:00:00-04:00", index=1),
    ])
    calendar = tmp_path.parent / f"{tmp_path.name}-early-close-calendar.json"
    calendar.write_text(json.dumps({
        "trading_sessions": ["2026-10-02"],
        "early_closes": {"2026-10-02": "13:00"},
    }), encoding="utf-8")
    early_result = run_verify(early_close, "--symbol", "SPX", "--calendar", str(calendar))
    early_report = json.loads(early_result.stdout)
    early_codes = {issue["code"] for issue in early_report["issues"]}
    gap = next(issue for issue in early_report["issues"] if issue["code"] == "capture_gap")
    assert "noncollection_interval" in early_codes
    assert gap["to"] == "2026-10-02T13:00:00-04:00"


def test_verifier_rejects_path_traversal_symbol(tmp_path):
    result = run_verify(tmp_path, "--symbol", "../../outside")
    assert result.returncode == 2
    assert not (tmp_path.parent / "outside").exists()


def test_verifier_stops_at_configured_resource_limits(tmp_path):
    write_session(tmp_path)

    bytes_limited = run_verify(tmp_path, "--max-total-bytes", "1")
    records_limited = run_verify(tmp_path, "--max-records", "1")

    assert bytes_limited.returncode == 2
    assert json.loads(bytes_limited.stdout)["status"] == "unavailable"
    assert records_limited.returncode == 2
    assert json.loads(records_limited.stdout)["status"] == "unavailable"

    second_session = [frame(stamp="2026-10-02T09:30:00-04:00", index=0)]
    write_session(tmp_path, "SPX", "2026-10-02", second_session)
    files_limited = run_verify(tmp_path, "--max-files", "1")
    assert files_limited.returncode == 2
    assert json.loads(files_limited.stdout)["status"] == "unavailable"


def test_verifier_reports_inaccessible_symbol_directory_as_unavailable(tmp_path):
    write_session(tmp_path)
    symbol_dir = tmp_path / "SPX"
    symbol_dir.chmod(0)
    try:
        result = run_verify(tmp_path)
    finally:
        symbol_dir.chmod(0o700)

    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "unavailable"
