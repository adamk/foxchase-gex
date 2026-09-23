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
                "online": 3,
                "client_cached": False,
                "client_cache_age_seconds": 0,
            }

    captured_headers = {}

    def fake_post(_url, headers, json, timeout):
        captured_headers.update(headers)
        assert timeout == 55
        return Response()

    causal_input = {"symbol":"SPX", "spot":7752.5,
                    "expiration_date":datetime.now(NY).date().isoformat(),
                    "contracts":[{"option_type":"CALL","strike":7750,
                                  "open_interest":100,"gamma":.004,
                                  "volatility":.18,"multiplier":100}]}
    monkeypatch.setattr("gex_client.collector.fetch_sanitized_snapshot", lambda symbol: causal_input)
    authenticated = []
    monkeypatch.setattr(
        "gex_client.collector.record_authenticated_success",
        lambda: authenticated.append(True),
    )
    monkeypatch.setattr("gex_client.collector.requests.post", fake_post)
    collect_once("http://127.0.0.1:8765", "SPX", "collector-spx")

    saved = snapshot("SPX", datetime.now(NY).date().isoformat(), 0)
    assert authenticated == [True]
    assert captured_headers["X-GEX-Session"] == "collector-spx"
    assert "online" not in saved
    assert "client_cached" not in saved


def test_collector_status_failure_does_not_fail_archive(monkeypatch, tmp_path, capsys):
    def failed_status(*args, **kwargs):
        raise RuntimeError("private sink detail")

    monkeypatch.setattr("gex_client.collector.send_dashboard_status", failed_status)
    test_collector_archives_computed_result_without_transient_fields(monkeypatch, tmp_path)
    output = capsys.readouterr().out
    assert "auth status sync failed: RuntimeError" in output
    assert "private sink detail" not in output


def test_failed_authenticated_collection_cannot_promote_health(monkeypatch):
    from gex_client import collector
    import pytest

    def failed_fetch(symbol):
        raise RuntimeError("authentication failed")

    promoted = []
    monkeypatch.setattr(collector, "verify_archive_mount", lambda: None)
    monkeypatch.setattr(collector, "fetch_sanitized_snapshot", failed_fetch)
    monkeypatch.setattr(collector, "record_authenticated_success", lambda: promoted.append(True))
    with pytest.raises(RuntimeError, match="authentication failed"):
        collect_once("http://127.0.0.1:8765", "SPX", "fixture")
    assert promoted == []
