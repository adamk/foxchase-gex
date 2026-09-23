import json
from datetime import datetime
from zoneinfo import ZoneInfo

from gex_client.forward_audit import archive_forward_audit


NY = ZoneInfo("America/New_York")


def snapshot(put_wall=7380, call_wall=7420):
    return {
        "display_symbol": "SPX", "spot": 7400,
        "updated_at": "2026-08-21T10:00:00-04:00", "strikes": [],
        "structure": {
            "net_gex": 100, "gamma_regime": "Positive", "gamma_flip": 7375,
            "spot_minus_flip": 25, "flip_distance_pct": .338,
            "call_wall": call_wall, "call_wall_strength": "Strong", "call_wall_share": .3,
            "put_wall": put_wall, "put_wall_strength": "Strong", "put_wall_share": .3,
            "gex_imbalance": 1.2,
        },
    }


def test_shadow_audit_records_features_decisions_and_migration(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    stamp = datetime(2026, 8, 21, 10, 0, tzinfo=NY)
    path = archive_forward_audit("SPX", snapshot(), stamp)
    archive_forward_audit("SPX", snapshot(put_wall=7390), stamp.replace(minute=1))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["shadow_decisions"]["bull_put"]["action"] == "ALLOW"
    assert rows[0]["shadow_decisions"]["iron_condor"]["action"] == "ALLOW"
    assert rows[1]["features"]["put_wall_migration"] == "up"


def test_shadow_blocks_negative_gamma_and_near_flip(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    data = snapshot()
    data["structure"].update({
        "gamma_regime": "Negative", "net_gex": -20,
        "gamma_flip": 7395, "spot_minus_flip": 5, "flip_distance_pct": .068,
    })
    path = archive_forward_audit("SPX", data, datetime(2026, 8, 22, 10, 0, tzinfo=NY))
    row = json.loads(path.read_text().strip())
    assert row["shadow_decisions"]["bull_put"]["action"] == "BLOCK"
    assert row["shadow_decisions"]["iron_condor"]["action"] == "BLOCK"


def test_audit_attaches_only_safe_same_day_algo_context(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path / "archive"))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "trade_date": "2026-08-21", "entered_today": True,
        "last_trigger_time": "2026-08-21T10:05:00-04:00",
        "last_entry_attempt": {"setup": "R5_TEST", "result": {"private": "omitted"}},
    }))
    monkeypatch.setenv("FOXCHASE_ALGO_STATE_PATH", str(state))
    path = archive_forward_audit("SPX", snapshot(), datetime(2026, 8, 21, 10, 5, tzinfo=NY))
    row = json.loads(path.read_text().strip())
    assert row["algo_context"] == {
        "trade_date": "2026-08-21", "entered_today": True,
        "active_or_attempted_setup": "R5_TEST",
        "last_trigger_time": "2026-08-21T10:05:00-04:00",
    }
    assert "private" not in json.dumps(row)
