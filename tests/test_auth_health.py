import json
from datetime import datetime, timezone

from gex_client import auth_health
from gex_client import schwab


def epoch(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_AUTH_HEALTH_PATH", str(tmp_path / "health.json"))


def test_healthy_and_warning_windows(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    start = epoch("2026-08-31T12:00:00")
    state = auth_health.record_interactive_authorization(start)
    assert auth_health.authorization_state(state, start)["health"] == "healthy"
    assert auth_health.due_alert_kind(state, start + 4 * 86400 + 1) == "72h"
    assert auth_health.due_alert_kind(state, start + 6 * 86400 + 1) == "24h"
    assert auth_health.due_alert_kind(state, start + 7 * 86400 + 1) == "urgent"


def test_refresh_does_not_extend_interactive_deadline(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    start = epoch("2026-08-31T12:00:00")
    original = auth_health.record_interactive_authorization(start)
    refreshed = auth_health.record_refresh_success(start + 3 * 86400)
    assert refreshed["interactive_authorized_at"] == original["interactive_authorized_at"]
    assert refreshed["reauthorization_due_at"] == original["reauthorization_due_at"]


def test_invalid_grant_marks_required_without_secret(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "client")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "secret")
    class Response:
        ok = False
        status_code = 400
        text = '{"error":"invalid_grant"}'
        def json(self): return {"error": "invalid_grant"}
    monkeypatch.setattr(schwab.requests, "post", lambda *a, **k: Response())
    try:
        schwab._refresh_tokens({"refresh_token": "private-refresh"})
    except schwab.SchwabError:
        pass
    state = auth_health.load_status()
    assert state["health"] == "reauthorization_required"
    assert "private-refresh" not in json.dumps(state)
    assert "secret" not in json.dumps(state)


def test_alert_debounce_and_reauthorization_reset(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("FOXCHASE_DASHBOARD_EVENT_URL", "https://private.example/api/bot-events")
    monkeypatch.setenv("FOXCHASE_DASHBOARD_EVENT_TOKEN", "private-token")
    start = epoch("2026-08-31T12:00:00")
    auth_health.record_interactive_authorization(start)
    calls = []
    class Response: ok = True
    monkeypatch.setattr(auth_health.requests, "post", lambda *a, **k: calls.append(k) or Response())
    warning_time = start + 4 * 86400 + 1
    assert auth_health.emit_due_alert(warning_time) == "72h"
    assert auth_health.emit_due_alert(warning_time) is None
    assert len(calls) == 1
    auth_health.record_auth_failure("invalid_grant", warning_time)
    reset = auth_health.record_interactive_authorization(warning_time + 60)
    assert reset["health"] == "healthy"
    assert reset["alerts_sent"] == {}
    assert reset["recovery_pending"] is True
    assert "private-token" not in json.dumps(reset)


def test_successful_refresh_rotation_and_no_token_in_metadata(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "client")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "secret")
    auth_health.record_interactive_authorization(1000)
    class Response:
        ok = True
        status_code = 200
        text = "ok"
        def json(self):
            return {"access_token":"new-access", "refresh_token":"rotated-refresh", "expires_in":1800}
    monkeypatch.setattr(schwab.requests, "post", lambda *a, **k: Response())
    result = schwab._refresh_tokens({"refresh_token":"old-refresh"})
    assert result["refresh_token"] == "rotated-refresh"
    metadata = json.dumps(auth_health.load_status())
    assert "rotated-refresh" not in metadata and "new-access" not in metadata


def test_no_external_channel_does_not_mark_delivered(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    start = epoch("2026-08-31T12:00:00")
    auth_health.record_interactive_authorization(start)
    assert auth_health.emit_due_alert(start + 5 * 86400) is None
    assert auth_health.load_status()["alerts_sent"] == {}
