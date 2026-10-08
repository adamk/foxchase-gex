import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from gex_client import auth_health, auth_lifecycle, schwab
from gex_client import local_setup


def epoch(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def configure(monkeypatch, tmp_path):
    token_file = tmp_path / "schwab_tokens.json"
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(token_file))
    monkeypatch.setenv("SCHWAB_AUTH_HEALTH_PATH", str(tmp_path / "health.json"))
    monkeypatch.setenv("SCHWAB_AUTH_LIFECYCLE_PATH", str(tmp_path / "lifecycle.json"))
    return token_file


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [
        (0, "healthy"),
        (5.9, "healthy"),
        (6, "due_soon"),
        (7, "reauth_required"),
    ],
)
def test_operator_status_day_thresholds(monkeypatch, tmp_path, age_days, expected):
    configure(monkeypatch, tmp_path)
    start = epoch("2026-10-08T12:00:00")
    auth_lifecycle.record_full_oauth_authorization(start)

    status = auth_lifecycle.operator_status(start + age_days * 86400)

    assert status["state"] == expected
    assert set(status) == {
        "state", "last_full_oauth_at", "next_reauth_due_at",
        "remaining_seconds", "reauth_required",
    }


def test_invalid_grant_latch_forces_reauthorization(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    start = epoch("2026-10-08T12:00:00")
    auth_lifecycle.record_full_oauth_authorization(start)
    auth_health.record_auth_failure("invalid_grant", start + 60)

    status = auth_lifecycle.operator_status(start + 3600)

    assert status["state"] == "reauth_required"
    assert status["reauth_required"] is True
    assert status["remaining_seconds"] > 0


def test_legacy_health_timestamp_migrates_to_exact_two_field_record(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    start = epoch("2026-10-08T12:00:00")
    auth_health.record_interactive_authorization(start)

    lifecycle = auth_lifecycle.load_lifecycle(migrate_legacy=True)

    assert lifecycle == {
        "last_full_oauth_at": auth_health._iso(start),
        "next_reauth_due_at": auth_health._iso(start + 7 * 86400),
    }
    assert json.loads((tmp_path / "lifecycle.json").read_text()) == lifecycle


def test_malformed_lifecycle_fails_closed_without_legacy_fallback(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    start = epoch("2026-10-08T12:00:00")
    auth_health.record_interactive_authorization(start)
    (tmp_path / "lifecycle.json").write_text(
        json.dumps({"last_full_oauth_at": auth_health._iso(start), "extra": "bad"}),
        encoding="utf-8",
    )

    status = auth_lifecycle.operator_status(start + 60)

    assert status["state"] == "reauth_required"
    assert status["last_full_oauth_at"] is None
    assert status["next_reauth_due_at"] is None


def test_access_token_refresh_does_not_reset_full_oauth_lifecycle(monkeypatch, tmp_path):
    token_file = configure(monkeypatch, tmp_path)
    start = epoch("2026-10-08T12:00:00")
    initial = auth_lifecycle.record_full_oauth_authorization(start)
    token_file.write_text(json.dumps({
        "access_token": "expired-access",
        "refresh_token": "refresh-secret",
        "saved_at": 0,
        "expires_in": 1800,
    }), encoding="utf-8")
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "client-secret")

    class Response:
        ok = True
        status_code = 200
        text = "ok"

        def json(self):
            return {"access_token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 1800}

    monkeypatch.setattr(schwab.requests, "post", lambda *a, **k: Response())
    schwab._refresh_tokens(json.loads(token_file.read_text(encoding="utf-8")))

    assert auth_lifecycle.load_lifecycle() == initial


def test_successful_full_oauth_exchange_resets_lifecycle(monkeypatch, tmp_path):
    token_file = configure(monkeypatch, tmp_path)
    start = epoch("2026-10-08T12:00:00")
    auth_lifecycle.record_full_oauth_authorization(start)
    monkeypatch.setattr(auth_lifecycle.time, "time", lambda: start + 3 * 86400)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "client-secret")

    class Response:
        ok = True
        status_code = 200
        text = "ok"

        def json(self):
            return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 1800}

    monkeypatch.setattr(schwab.requests, "post", lambda *a, **k: Response())
    schwab.exchange_authorization_response("one-time-code")

    lifecycle = json.loads((tmp_path / "lifecycle.json").read_text(encoding="utf-8"))
    assert set(lifecycle) == {"last_full_oauth_at", "next_reauth_due_at"}
    assert lifecycle["last_full_oauth_at"] == auth_health._iso(start + 3 * 86400)
    assert lifecycle["next_reauth_due_at"] == auth_health._iso(start + 10 * 86400)
    assert token_file.exists()
    assert (tmp_path / "lifecycle.json").stat().st_mode & 0o777 == 0o600


def test_existing_oauth_exchange_reads_gui_stored_credentials(monkeypatch, tmp_path):
    token_file = configure(monkeypatch, tmp_path)
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    monkeypatch.delenv("SCHWAB_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SCHWAB_REDIRECT_URI", raising=False)
    monkeypatch.setenv("SCHWAB_CREDENTIALS_PATH", str(tmp_path / "schwab_credentials.json"))
    local_setup.save_credentials("gui-app-key", "gui-app-secret", "https://127.0.0.1")
    seen = {}

    class Response:
        ok = True
        status_code = 200
        text = "ok"

        def json(self):
            return {"access_token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 1800}

    def post(url, *, headers, data, timeout):
        seen.update(url=url, headers=headers, data=data, timeout=timeout)
        return Response()

    monkeypatch.setattr(schwab.requests, "post", post)
    oauth_state = "state-value-for-this-gui-attempt-0123456789"
    authorize_url = schwab.authorization_url(state=oauth_state)
    assert parse_qs(urlparse(authorize_url).query)["state"] == [oauth_state]
    schwab.exchange_authorization_response(
        f"https://127.0.0.1/?code=gui-code&state={oauth_state}",
        expected_state=oauth_state,
    )

    assert seen["url"] == schwab.TOKEN_URL
    assert seen["data"]["grant_type"] == "authorization_code"
    assert seen["data"]["code"] == "gui-code"
    assert seen["data"]["redirect_uri"] == "https://127.0.0.1"
    assert seen["headers"]["Authorization"].startswith("Basic ")
    assert token_file.exists()
    assert json.loads((tmp_path / "lifecycle.json").read_text())["next_reauth_due_at"]


def test_oauth_exchange_rejects_missing_or_wrong_state_before_network_call(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "client-secret")
    calls = []
    monkeypatch.setattr(schwab.requests, "post", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(schwab.SchwabError, match="state"):
        schwab.exchange_authorization_response(
            "https://127.0.0.1/?code=some-code&state=wrong", expected_state="expected"
        )
    with pytest.raises(schwab.SchwabError, match="state"):
        schwab.exchange_authorization_response(
            "https://127.0.0.1/?code=some-code", expected_state="expected"
        )

    assert calls == []
