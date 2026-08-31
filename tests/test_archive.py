from datetime import datetime
from zoneinfo import ZoneInfo

from gex_client.archive import archive_snapshot, list_sessions, snapshot, timeline
from gex_client.collector import collect_once


NY = ZoneInfo("America/New_York")


def computed(symbol="SPX", spot=7752.5):
    return {
        "display_symbol": symbol,
        "spot": spot,
        "updated_at": "2026-08-20T10:00:00-04:00",
        "strikes": [{"strike": 7750.0, "gex": 0.26}],
        "patterns": {"primary": "Positive Gamma", "signals": []},
    }


def test_archive_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    archive_snapshot("SPX", computed(), datetime(2026, 8, 20, 10, 0, tzinfo=NY))
    archive_snapshot("SPX", computed(spot=7754), datetime(2026, 8, 20, 10, 1, tzinfo=NY))

    assert list_sessions("SPX")[0]["date"] == "2026-08-20"
    assert list_sessions("SPX")[0]["captures"] == 2
    points = timeline("SPX", "2026-08-20")
    assert [point["spot"] for point in points] == [7752.5, 7754]
    result = snapshot("SPX", "2026-08-20", -1)
    assert result["spot"] == 7754
    assert result["historical"] is True
    assert result["captured_at"] == "2026-08-20T10:01:00-04:00"


def test_archive_rejects_symbol_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    try:
        archive_snapshot("SPX", computed(symbol="NDX"))
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("symbol mismatch was accepted")


def test_collector_archives_computed_result_without_transient_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FOXCHASE_GEX_REQUIRED_MOUNT", raising=False)

    class Response:
        ok = True

        def json(self):
            return {
                **computed(),
                "_archive_causal_input": {
                    "symbol": "SPX", "spot": 7752.5,
                    "expiration_date": datetime.now(NY).date().isoformat(),
                    "contracts": [{"option_type":"CALL", "strike":7750,
                                   "open_interest":100, "gamma":.004,
                                   "volatility":.18, "multiplier":100}],
                },
                "online": 3,
                "client_cached": False,
                "client_cache_age_seconds": 0,
            }

    captured_headers = {}

    def fake_get(_url, headers, timeout):
        captured_headers.update(headers)
        assert timeout == 55
        return Response()

    monkeypatch.setattr("gex_client.collector.requests.get", fake_get)
    collect_once("http://127.0.0.1:8765", "SPX", "collector-spx")

    saved = snapshot("SPX", datetime.now(NY).date().isoformat(), 0)
    assert captured_headers["X-GEX-Session"] == "collector-spx"
    assert captured_headers["X-GEX-Archive-Inputs"] == "1"
    assert "online" not in saved
    assert "client_cached" not in saved
