import importlib
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gex_client.archive import archive_snapshot
from gex_client import live_replay


ET = ZoneInfo("America/New_York")
app_module = importlib.import_module("gex_client.app")
DAY = datetime(2026, 10, 2, 10, 0, tzinfo=ET)


def capture(tmp_path, symbol, minute, spot, *, causal_input=None):
    archive_snapshot(
        symbol,
        {"display_symbol": symbol, "spot": spot,
         "strikes": [{"strike": spot, "gex": spot / 10}]},
        datetime(2026, 10, 2, 9, minute, tzinfo=ET),
        causal_input=causal_input,
    )


def test_current_session_timeline_and_timestamped_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    capture(tmp_path, "SPX", 30, 6800, causal_input={"private_probe": "never-export"})
    capture(tmp_path, "SPX", 31, 6810)
    capture(tmp_path, "NDX", 30, 25000)
    monkeypatch.setattr(live_replay, "_now", lambda: DAY)
    monkeypatch.setitem(app_module.app.config, "LOCAL_SETUP_ENABLED", True)
    client = app_module.app.test_client()

    for symbol, expected in (("SPX", 2), ("NDX", 1)):
        response = client.get(f"/api/live-replay/{symbol}/timeline",
                              base_url="http://127.0.0.1:8765")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        points = response.json["points"]
        assert len(points) == expected
        assert points == sorted(points, key=lambda p: p["cursor"])
        latest = points[-1]["captured_at"]
        selected = client.get(f"/api/live-replay/{symbol}/snapshot",
                              query_string={"captured_at": latest},
                              base_url="http://127.0.0.1:8765")
        assert selected.status_code == 200
        assert selected.json["display_symbol"] == symbol
        assert selected.json["captured_at"] == latest
        assert selected.json["replay"] is True
        assert "causal_input" not in selected.json
        assert "never-export" not in selected.get_data(as_text=True)

    after = client.get("/api/live-replay/SPX/timeline?after=1",
                       base_url="http://127.0.0.1:8765")
    assert len(after.json["points"]) == 1


def test_duplicate_handling_and_conflict(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    path = archive_snapshot("SPX", {"display_symbol": "SPX", "spot": 6800,
                                    "strikes": [{"strike": 6800, "gex": 1}]},
                            datetime(2026, 10, 2, 9, 30, tzinfo=ET))
    first = path.read_bytes()
    with path.open("ab") as stream:
        stream.write(first)
    assert len(live_replay.timeline("SPX", now=DAY)["points"]) == 1
    capture(tmp_path, "SPX", 30, 6810)
    with pytest.raises(live_replay.ReplayUnavailable, match="conflicting"):
        live_replay.timeline("SPX", now=DAY)


def test_bad_hash_future_capture_and_incomplete_line(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    path = archive_snapshot("SPX", {"display_symbol": "SPX", "spot": 6800,
                                    "strikes": [{"strike": 6800, "gex": 1}]},
                            datetime(2026, 10, 2, 9, 30, tzinfo=ET))
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    assert len(live_replay.timeline("SPX", now=DAY)["points"]) == 1
    path.write_bytes(path.read_bytes().replace(b'"spot":6800', b'"spot":6801'))
    with pytest.raises(live_replay.ReplayUnavailable):
        live_replay.timeline("SPX", now=DAY)

    path.unlink()
    capture(tmp_path, "SPX", 31, 6810)
    with pytest.raises(live_replay.ReplayUnavailable):
        live_replay.timeline("SPX", now=datetime(2026, 10, 2, 9, 30, tzinfo=ET))


def test_hosted_gate_and_invalid_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    capture(tmp_path, "SPX", 30, 6800)
    monkeypatch.setattr(live_replay, "_now", lambda: DAY)
    monkeypatch.setitem(app_module.app.config, "LOCAL_SETUP_ENABLED", False)
    monkeypatch.setitem(app_module.app.config, "SECRET_KEY", "test-only-session-secret-32-bytes-long")
    monkeypatch.setattr(app_module, "_operator_auth_ready", lambda: True)
    client = app_module.app.test_client()
    assert client.get("/api/live-replay/SPX/timeline").status_code == 401
    with client.session_transaction() as session:
        session["operator_authenticated"] = True
    assert client.get("/api/live-replay/SPX/timeline").status_code == 200
    assert client.get("/api/live-replay/SPX/timeline?after=bad").status_code == 400
    assert client.get("/api/live-replay/SPX/snapshot?captured_at=wrong").status_code == 404
    assert client.get("/api/live-replay/OTHER/timeline").status_code == 400


def test_live_replay_slider_dom():
    script = Path(__file__).with_name("test_live_replay_ui.js")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True,
                            timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert "LIVE_REPLAY_UI_PASS" in result.stdout
