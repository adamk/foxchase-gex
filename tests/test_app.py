import importlib


app_module = importlib.import_module("gex_client.app")


def test_gex_route_connects_sanitized_snapshot_to_private_compute(monkeypatch):
    app_module._RESULT_CACHE.clear()
    snapshot = {
        "symbol": "SPX",
        "spot": 7752.5,
        "expiration_date": "2026-08-10",
        "contracts": [
            {
                "option_type": "CALL",
                "strike": 7750.0,
                "open_interest": 100.0,
                "gamma": 0.004,
                "volatility": 18.0,
                "multiplier": 100.0,
            }
        ],
    }
    computed = {
        "display_symbol": "SPX",
        "spot": 7752.5,
        "updated_at": "2026-08-10T10:00:00-04:00",
        "strikes": [{"strike": 7750.0, "gex": 0.26}],
        "patterns": {"primary": "Insufficient Data", "signals": []},
        "online": 3,
    }
    monkeypatch.setattr(app_module, "fetch_sanitized_snapshot", lambda symbol: snapshot)

    def fake_remote(method, path, session_id, payload=None):
        assert method == "POST"
        assert path == "gex"
        assert session_id == "route-test-session"
        assert payload == snapshot
        return 200, dict(computed)

    monkeypatch.setattr(app_module, "_remote_json", fake_remote)
    client = app_module.app.test_client()
    response = client.get(
        "/api/gex/SPX", headers={"X-GEX-Session": "route-test-session"}
    )

    assert response.status_code == 200
    assert response.json["display_symbol"] == "SPX"
    assert response.json["strikes"] == computed["strikes"]
    assert response.json["client_cached"] is False



def test_presence_route_proxies_anonymous_session(monkeypatch):
    def fake_remote(method, path, session_id, payload=None):
        assert (method, path, session_id) == (
            "POST",
            "presence",
            "presence-test-session",
        )
        return 200, {"online": 7, "ttl_seconds": 90}

    monkeypatch.setattr(app_module, "_remote_json", fake_remote)
    client = app_module.app.test_client()
    response = client.post(
        "/api/presence", headers={"X-GEX-Session": "presence-test-session"}
    )

    assert response.status_code == 200
    assert response.json == {"online": 7, "ttl_seconds": 90}


def test_dashboard_starts_with_zero_active_sessions():
    client = app_module.app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert '<span id="active-sessions">0</span>' in response.get_data(as_text=True)


def test_setup_status_never_returns_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "private-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "private-client-secret")
    monkeypatch.setattr(app_module, "token_path", lambda: tmp_path / "missing-token.json")
    client = app_module.app.test_client()

    response = client.get("/api/setup-status")

    assert response.status_code == 200
    assert response.json == {
        "ready": False,
        "client_id_configured": True,
        "client_secret_configured": True,
        "token_configured": False,
    }
    assert "private-client" not in response.get_data(as_text=True)


def test_history_routes(monkeypatch, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from gex_client.archive import archive_snapshot

    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    archive_snapshot(
        "NDX",
        {
            "display_symbol": "NDX",
            "spot": 25000,
            "strikes": [{"strike": 25000, "gex": -0.5}],
        },
        datetime(2026, 8, 20, 11, 0, tzinfo=ZoneInfo("America/New_York")),
    )
    client = app_module.app.test_client()

    sessions = client.get("/api/history/NDX/sessions")
    assert sessions.status_code == 200
    assert sessions.json["sessions"][0]["captures"] == 1
    timeline = client.get("/api/history/NDX/2026-08-20/timeline")
    assert timeline.status_code == 200
    assert timeline.json["timeline"][0]["spot"] == 25000
    result = client.get("/api/history/NDX/2026-08-20/snapshot?index=0")
    assert result.status_code == 200
    assert result.json["historical"] is True
