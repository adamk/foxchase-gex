import importlib
import re
from urllib.parse import parse_qs, urlparse

import pytest
from werkzeug.security import generate_password_hash

from gex_client import auth_health, auth_lifecycle


app_module = importlib.import_module("gex_client.app")


def configure_operator(monkeypatch, tmp_path):
    monkeypatch.setenv("GEX_OPERATOR_USERNAME", "operator-test")
    monkeypatch.setenv("GEX_OPERATOR_PASSWORD_HASH", generate_password_hash("operator-password"))
    monkeypatch.setenv("GEX_SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("SCHWAB_AUTH_LIFECYCLE_PATH", str(tmp_path / "lifecycle.json"))
    monkeypatch.setenv("SCHWAB_AUTH_HEALTH_PATH", str(tmp_path / "health.json"))
    for key, value in {
        "SECRET_KEY": "s" * 48,
        "SESSION_COOKIE_NAME": "gex_operator_session",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": True,
    }.items():
        monkeypatch.setitem(app_module.app.config, key, value)


def csrf_from(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match
    return match.group(1)


def login(client):
    page = client.get("/operator/login")
    assert page.status_code == 200
    token = csrf_from(page)
    return client.post("/operator/login", data={
        "csrf_token": token,
        "username": "operator-test",
        "password": "operator-password",
    })


def test_operator_endpoints_require_login(monkeypatch, tmp_path):
    configure_operator(monkeypatch, tmp_path)
    client = app_module.app.test_client()

    status = client.get("/api/operator/auth-status")
    reauthorize = client.get("/operator/reauthorize-schwab")

    assert status.status_code == 401
    assert reauthorize.status_code == 401


def test_status_payload_is_allowlisted_and_authenticated(monkeypatch, tmp_path):
    configure_operator(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "client-secret")
    start = 1791460800.0
    auth_lifecycle.record_full_oauth_authorization(start)
    auth_health._write({
        "health": "healthy",
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "unrelated": "token-file-content",
    })
    client = app_module.app.test_client()
    assert login(client).status_code == 303

    response = client.get("/api/operator/auth-status")

    assert response.status_code == 200
    assert set(response.json) == {
        "state", "last_full_oauth_at", "next_reauth_due_at",
        "remaining_seconds", "reauth_required",
    }
    assert response.json["state"] == "healthy"
    encoded = response.get_data(as_text=True)
    for secret in ("access-secret", "refresh-secret", "client-secret", "token-file-content"):
        assert secret not in encoded
    assert response.headers["Cache-Control"] == "no-store"


def test_login_uses_csrf_and_rotates_cookie_session(monkeypatch, tmp_path):
    configure_operator(monkeypatch, tmp_path)
    client = app_module.app.test_client()
    page = client.get("/operator/login")
    old_cookie = page.headers["Set-Cookie"].split(";", 1)[0]
    response = client.post("/operator/login", data={
        "csrf_token": csrf_from(page),
        "username": "operator-test",
        "password": "operator-password",
    })

    assert response.status_code == 303
    new_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
    assert old_cookie != new_cookie
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "SameSite=Lax" in response.headers["Set-Cookie"]
    assert "Secure" in response.headers["Set-Cookie"]


def test_login_rejects_bad_csrf_and_credentials_without_logging_values(monkeypatch, tmp_path, caplog):
    configure_operator(monkeypatch, tmp_path)
    client = app_module.app.test_client()
    page = client.get("/operator/login")

    bad_csrf = client.post("/operator/login", data={
        "csrf_token": "wrong",
        "username": "operator-test",
        "password": "operator-password",
    })
    assert bad_csrf.status_code == 400

    page = client.get("/operator/login")
    bad_password = client.post("/operator/login", data={
        "csrf_token": csrf_from(page),
        "username": "operator-test",
        "password": "wrong-password",
    })
    assert bad_password.status_code == 401
    assert "wrong-password" not in caplog.text
    assert "operator-password" not in caplog.text

    unicode_csrf = client.post("/operator/login", data={
        "csrf_token": "à",
        "username": "operator-test",
        "password": "operator-password",
    })
    assert unicode_csrf.status_code == 400


def test_reauthorization_redirect_uses_existing_configured_url(monkeypatch, tmp_path):
    configure_operator(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "do-not-disclose-client-secret")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/callback")
    client = app_module.app.test_client()
    assert login(client).status_code == 303

    response = client.get("/operator/reauthorize-schwab")

    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["Location"]).query)
    assert query == {
        "client_id": ["public-client-id"],
        "redirect_uri": ["https://127.0.0.1/callback"],
    }
    assert "do-not-disclose-client-secret" not in response.headers["Location"]
    assert response.headers["Cache-Control"] == "no-store"


def test_logout_requires_csrf_and_clears_operator_session(monkeypatch, tmp_path):
    configure_operator(monkeypatch, tmp_path)
    client = app_module.app.test_client()
    assert login(client).status_code == 303
    page = client.get("/operator/logout")
    assert page.status_code == 200

    response = client.post("/operator/logout", data={"csrf_token": csrf_from(page)})

    assert response.status_code == 303
    assert client.get("/api/operator/auth-status").status_code == 401


def test_public_chart_and_gex_api_remain_available_without_operator_login(monkeypatch):
    app_module._RESULT_CACHE.clear()
    monkeypatch.setattr(app_module, "fetch_sanitized_snapshot", lambda symbol: {"symbol": symbol})

    def fake_remote(method, path, session_id, payload=None):
        return 200, {"display_symbol": path[-3:], "strikes": []}

    monkeypatch.setattr(app_module, "_remote_json", fake_remote)
    client = app_module.app.test_client()

    assert client.get("/").status_code == 200
    for symbol in ("NDX", "SPX"):
        response = client.get(f"/api/gex/{symbol}", headers={"X-GEX-Session": f"public-{symbol}"})
        assert response.status_code == 200


def test_operator_auth_configuration_missing_fails_closed_but_public_site_works(monkeypatch):
    monkeypatch.delenv("GEX_OPERATOR_USERNAME", raising=False)
    monkeypatch.delenv("GEX_OPERATOR_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("GEX_SESSION_SECRET", raising=False)
    monkeypatch.setitem(app_module.app.config, "SECRET_KEY", None)
    client = app_module.app.test_client()

    assert client.get("/").status_code == 200
    assert client.get("/operator/login").status_code == 503
    assert client.get("/api/operator/auth-status").status_code == 503


def test_operator_status_routes_do_not_return_unexpected_lifecycle_fields(monkeypatch, tmp_path):
    configure_operator(monkeypatch, tmp_path)
    start = 1791460800.0
    auth_lifecycle.record_full_oauth_authorization(start)
    lifecycle_path = tmp_path / "lifecycle.json"
    lifecycle_path.write_text(
        '{"last_full_oauth_at":"2026-10-08T00:00:00+00:00",'
        '"next_reauth_due_at":"2026-10-15T00:00:00+00:00",'
        '"access_token":"never-return"}',
        encoding="utf-8",
    )
    client = app_module.app.test_client()
    assert login(client).status_code == 303
    response = client.get("/api/operator/auth-status")
    assert response.status_code == 200
    assert response.json["state"] == "reauth_required"
    assert "never-return" not in response.get_data(as_text=True)
