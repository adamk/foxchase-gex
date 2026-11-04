"""The checked-in examples contain synthetic data only."""

import json
import subprocess
import sys
from pathlib import Path

from gex_client.archive import list_sessions, snapshot, timeline
from gex_client.history_verify import verify_archive


REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "examples" / "synthetic_archive"


def test_synthetic_fixtures_cover_both_symbols_and_multiple_sessions():
    report = verify_archive(
        FIXTURES,
        cadence_seconds=60,
        calendar_file=REPO / "examples" / "synthetic_calendar.json",
    )

    assert report["status"] == "valid"
    assert report["valid_captures"] == 18
    assert report["publisher_readiness"]["redistribution"] == "synthetic_claim_unverified"
    assert report["symbols"]["SPX"]["session_count"] == 3
    assert report["symbols"]["NDX"]["session_count"] == 3


def test_existing_local_history_routes_replay_synthetic_archive(monkeypatch):
    from gex_client import app as app_module

    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(FIXTURES))

    assert [row["date"] for row in list_sessions("SPX")] == [
        "2026-10-05", "2026-10-02", "2026-10-01",
    ]
    points = timeline("NDX", "2026-10-01")
    assert len(points) == 3
    assert points[-1]["read"] == "Synthetic positive gamma"
    restored = snapshot("NDX", "2026-10-01", -1)
    assert restored["historical"] is True
    assert restored["display_symbol"] == "NDX"

    client = app_module.app.test_client()
    sessions = client.get("/api/history/NDX/sessions")
    replay = client.get("/api/history/NDX/2026-10-01/timeline")
    selected = client.get("/api/history/NDX/2026-10-01/snapshot?index=2")
    assert sessions.status_code == replay.status_code == selected.status_code == 200
    assert sessions.json["sessions"][0]["date"] == "2026-10-05"
    assert replay.json["timeline"][-1]["read"] == "Synthetic positive gamma"
    assert selected.json["historical"] is True


def test_synthetic_archive_can_be_exported_without_external_services(tmp_path):
    output = tmp_path / "publication"
    result = subprocess.run(
        [sys.executable, "-m", "gex_client.history_export",
         "--archive-dir", str(FIXTURES), "--output-dir", str(output),
         "--calendar", str(REPO / "examples" / "synthetic_calendar.json"),
         "--allow-synthetic-fixtures", "--dry-run", "--format", "json"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "ready"
    assert report["session_count"] == 6
    assert report["capture_count"] == 18
    assert not output.exists()
