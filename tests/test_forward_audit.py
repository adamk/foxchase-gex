import json
import subprocess
import sys
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
        "gex_forward_context": {},
    }
    assert "private" not in json.dumps(row)


def test_collector_entrypoint_imports_from_fresh_checkout():
    result = subprocess.run(
        [sys.executable, "-m", "gex_client.collector", "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--interval" in result.stdout


def test_v2_context_produces_atomic_shadow_summary(monkeypatch, tmp_path):
    from gex_client import forward_audit
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path / "archive"))
    state = tmp_path / "state.json"
    context = {"regime": "R1", "spy_open": 740,
               "raw_spx_parity_center": 7400, "raw_spx_em_lower": 7380,
               "spx_mapped_em_lower": 738}
    state.write_text(json.dumps({"trade_date": "2026-08-21", "gex_forward_context": context}))
    monkeypatch.setenv("FOXCHASE_ALGO_STATE_PATH", str(state))
    published = []
    monkeypatch.setattr(forward_audit, "_publish_r1_summary", published.append)
    path = archive_forward_audit("SPX", snapshot(), datetime(2026, 8, 21, 10, 5, tzinfo=NY))
    row = json.loads(path.read_text())
    assert row["features"]["method_version"] == "foxchase_shadow_gex_v2"
    assert row["algo_context"]["gex_forward_context"] == context
    assert row["shadow_decisions"]["r1_spx_bull_put_confluence"]["action"] == "CANDIDATE"
    summary = tmp_path / "archive/audit/strategy/r1_spx_call_confluence/2026-08-21.json"
    assert json.loads(summary.read_text()) == published[-1]
    assert not summary.with_suffix('.tmp').exists()


def test_optional_telemetry_is_disabled_without_credentials(monkeypatch):
    from gex_client import forward_audit
    monkeypatch.delenv("FOXCHASE_DASHBOARD_EVENT_TOKEN", raising=False)
    monkeypatch.setenv("FOXCHASE_GEX_FORWARD_DASHBOARD_URL", "https://example.invalid/telemetry")
    monkeypatch.setattr(forward_audit.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError('network called')))
    forward_audit._publish_r1_summary({"status": "OPEN"})


def test_old_or_malformed_context_is_not_attached(monkeypatch, tmp_path):
    from gex_client.forward_audit import _algo_context
    state = tmp_path / "state.json"
    monkeypatch.setenv("FOXCHASE_ALGO_STATE_PATH", str(state))
    assert _algo_context("2026-08-21") is None
    state.write_text('{bad json')
    assert _algo_context("2026-08-21") is None
    state.write_text(json.dumps({"trade_date": "2026-08-20"}))
    assert _algo_context("2026-08-21") is None
