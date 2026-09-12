import json
import threading
import time
from datetime import datetime, timezone

import pytest

from gex_client import auth_health
from gex_client import schwab


def epoch(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_AUTH_HEALTH_PATH", str(tmp_path / "health.json"))
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "tokens.json"))


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


def test_wrapped_invalid_grant_marks_required(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "client")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "secret")

    class Response:
        ok = False
        status_code = 400
        text = '{"error":"unsupported_token_type"}'

        def json(self):
            return {
                "error": "unsupported_token_type",
                "error_description": json.dumps(
                    {"error": "invalid_grant", "error_description": "revoked"}
                ),
            }

    monkeypatch.setattr(schwab.requests, "post", lambda *a, **k: Response())
    with pytest.raises(schwab.SchwabError):
        schwab._refresh_tokens({"refresh_token": "private-refresh"})
    assert auth_health.load_status()["health"] == "reauthorization_required"
    assert auth_health.load_status()["last_failure_class"] == "invalid_grant"


def test_expired_access_fails_fast_after_permanent_auth_failure(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "tokens.json"))
    schwab.token_path().write_text(
        json.dumps({"access_token": "expired", "refresh_token": "old", "saved_at": 0}),
        encoding="utf-8",
    )
    auth_health.record_auth_failure("invalid_grant")
    calls = []
    monkeypatch.setattr(schwab.requests, "post", lambda *a, **k: calls.append(1))
    with pytest.raises(schwab.SchwabError, match="reauthorization required"):
        schwab.get_access_token()
    assert calls == []


def test_refresh_lock_reuses_rotated_token_after_concurrent_refresh(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    token_file = tmp_path / "tokens.json"
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(token_file))
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "client")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "secret")
    token_file.write_text(
        json.dumps({
            "access_token": "expired",
            "refresh_token": "old-refresh",
            "expires_in": 1800,
            "saved_at": 0,
        }),
        encoding="utf-8",
    )
    barrier = threading.Barrier(2)
    calls = []

    class Response:
        ok = True
        status_code = 200
        text = "ok"

        def json(self):
            return {
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 1800,
            }

    def post(*args, **kwargs):
        calls.append(kwargs["data"]["refresh_token"])
        time.sleep(0.05)
        return Response()

    monkeypatch.setattr(schwab.requests, "post", post)
    results = []

    def refresh():
        barrier.wait()
        results.append(schwab._refresh_tokens({"refresh_token": "old-refresh"}))

    threads = [threading.Thread(target=refresh) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert calls == ["old-refresh"]
    assert [result["access_token"] for result in results] == ["new-access", "new-access"]
    assert json.loads(token_file.read_text(encoding="utf-8"))["refresh_token"] == "rotated-refresh"
    assert (tmp_path / "tokens.json.lock").stat().st_mode & 0o777 == 0o600


def test_refresh_request_uses_schwab_form_semantics(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "client")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "secret")
    captured = {}

    class Response:
        ok = True
        status_code = 200
        text = "ok"

        def json(self):
            return {"access_token": "new-access", "expires_in": 1800}

    def post(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return Response()

    monkeypatch.setattr(schwab.requests, "post", post)
    schwab._refresh_tokens({"refresh_token": "old-refresh"})
    assert captured["url"] == schwab.TOKEN_URL
    assert captured["kwargs"]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert captured["kwargs"]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
    }
    assert captured["kwargs"]["headers"]["Authorization"].startswith("Basic ")


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


def test_dashboard_status_is_sanitized(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("FOXCHASE_DASHBOARD_EVENT_URL", "https://private.example/api/bot-events")
    monkeypatch.setenv("FOXCHASE_DASHBOARD_EVENT_TOKEN", "private-token")
    auth_health.record_interactive_authorization(epoch("2026-08-31T12:00:00"))
    captured = {}
    class Response: ok = True
    def post(url, **kwargs):
        captured.update(url=url, payload=kwargs["json"])
        return Response()
    monkeypatch.setattr(auth_health.requests, "post", post)
    assert auth_health.send_dashboard_status(True, epoch("2026-09-01T12:00:00"))
    assert captured["url"].endswith("/api/gex-auth-status")
    encoded = json.dumps(captured["payload"])
    assert set(captured["payload"]) == {"state", "authorization_due_at", "last_check_at", "collector_available"}
    assert "private-token" not in encoded
