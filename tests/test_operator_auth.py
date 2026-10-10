import importlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader
from werkzeug.security import generate_password_hash

from gex_client import auth_health, auth_lifecycle, schwab


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


def install_hosted_template(monkeypatch, tmp_path):
    template_dir = tmp_path / "static" / "templates"
    template_dir.mkdir(parents=True)
    source = Path(app_module.__file__).resolve().parents[1] / "deploy" / "production-ui" / "index.html"
    (template_dir / "index.html").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(app_module, "local_mode", False)
    monkeypatch.setattr(app_module, "_HOSTED_TEMPLATE_DIRECTORY", template_dir)
    monkeypatch.setattr(
        app_module.app.jinja_env,
        "loader",
        ChoiceLoader([
            PrefixLoader({"hosted": FileSystemLoader(str(template_dir))}),
            app_module._PACKAGE_TEMPLATE_LOADER,
        ]),
    )
    app_module.app.jinja_env.cache.clear()


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
    complete = client.post("/operator/complete-schwab-reauthorization")

    assert status.status_code == 401
    assert reauthorize.status_code == 401
    assert complete.status_code == 401


def test_hosted_dashboard_loads_installed_template_and_pending_form_only_for_operator(
    monkeypatch, tmp_path
):
    configure_operator(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "client-secret")
    install_hosted_template(monkeypatch, tmp_path)
    client = app_module.app.test_client()

    public_page = client.get("/").get_data(as_text=True)
    assert "Schwab reauthorization in progress" not in public_page
    assert 'id="operator-reauth-completion"' not in public_page

    assert login(client).status_code == 303
    authenticated_response = client.get("/")
    authenticated_page = authenticated_response.get_data(as_text=True)
    assert 'id="operator-reauth-completion"' not in authenticated_page
    assert authenticated_response.headers["Cache-Control"] == "no-store"
    assert authenticated_response.headers["Referrer-Policy"] == "no-referrer"

    assert client.get("/operator/reauthorize-schwab").status_code == 302
    pending_page = client.get("/").get_data(as_text=True)
    assert "Schwab reauthorization in progress" in pending_page
    assert 'action="/operator/complete-schwab-reauthorization"' in pending_page


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
        "remaining_seconds", "reauth_required", "reauthorization_pending",
    }
    assert response.json["state"] == "healthy"
    assert response.json["reauthorization_pending"] is False
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
    assert set(query) == {"client_id", "redirect_uri", "state"}
    assert query["client_id"] == ["public-client-id"]
    assert query["redirect_uri"] == ["https://127.0.0.1/callback"]
    assert len(query["state"][0]) >= 32
    with client.session_transaction() as operator_session:
        assert operator_session["operator_schwab_oauth_state"] == query["state"][0]
    assert "do-not-disclose-client-secret" not in response.headers["Location"]
    assert response.headers["Cache-Control"] == "no-store"


def test_restarting_hosted_reauthorization_replaces_pending_state(monkeypatch, tmp_path):
    configure_operator(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "client-secret")
    client = app_module.app.test_client()
    assert login(client).status_code == 303

    first = client.get("/operator/reauthorize-schwab")
    second = client.get("/operator/reauthorize-schwab")
    first_state = parse_qs(urlparse(first.headers["Location"]).query)["state"][0]
    second_state = parse_qs(urlparse(second.headers["Location"]).query)["state"][0]

    assert second_state != first_state
    with client.session_transaction() as operator_session:
        assert operator_session["operator_schwab_oauth_state"] == second_state
    assert client.get("/api/operator/auth-status").json["reauthorization_pending"] is True


def test_hosted_completion_requires_csrf_and_rejects_callback_format(monkeypatch, tmp_path):
    configure_operator(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "client-secret")
    exchange_calls = []
    monkeypatch.setattr(
        schwab, "exchange_authorization_response",
        lambda *args, **kwargs: exchange_calls.append((args, kwargs)),
    )
    client = app_module.app.test_client()
    assert login(client).status_code == 303
    client.get("/operator/reauthorize-schwab")
    csrf = csrf_from(client.get("/operator/login"))

    missing_csrf = client.post("/operator/complete-schwab-reauthorization", data={
        "authorization_response": "https://127.0.0.1/?code=one&state=valid-state-value-0123456789",
    })
    malformed = client.post("/operator/complete-schwab-reauthorization", data={
        "csrf_token": csrf,
        "authorization_response": "one-time-code",
    })
    missing_state = client.post("/operator/complete-schwab-reauthorization", data={
        "csrf_token": csrf,
        "authorization_response": "https://127.0.0.1/?code=one-time-code",
    })

    assert missing_csrf.status_code == 400
    assert malformed.status_code == 303
    assert missing_state.status_code == 303
    assert exchange_calls == []
    with client.session_transaction() as operator_session:
        assert operator_session["operator_reauthorization_notice"] == "state_mismatch"


def test_hosted_completion_rejects_old_state_before_provider_request(monkeypatch, tmp_path, caplog):
    configure_operator(monkeypatch, tmp_path)
    install_hosted_template(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "client-secret")
    provider_posts = []
    monkeypatch.setattr(schwab.requests, "post", lambda *a, **k: provider_posts.append((a, k)))
    client = app_module.app.test_client()
    assert login(client).status_code == 303
    first = client.get("/operator/reauthorize-schwab")
    stale_state = parse_qs(urlparse(first.headers["Location"]).query)["state"][0]
    client.get("/operator/reauthorize-schwab")
    csrf = csrf_from(client.get("/operator/login"))
    code = "never-log-hosted-auth-code"

    response = client.post("/operator/complete-schwab-reauthorization", data={
        "csrf_token": csrf,
        "authorization_response": f"https://127.0.0.1/?code={code}&state={stale_state}",
    })

    assert response.status_code == 303
    assert provider_posts == []
    assert code not in caplog.text
    assert "state_mismatch" in caplog.text
    assert client.get("/api/operator/auth-status").json["reauthorization_pending"] is True
    stale_page = client.get("/").get_data(as_text=True)
    assert "This authorization no longer matches the current session." in stale_page
    assert code not in stale_page


def test_hosted_completion_reuses_oauth_exchange_and_updates_lifecycle_after_validation(
    monkeypatch, tmp_path, caplog
):
    configure_operator(monkeypatch, tmp_path)
    install_hosted_template(monkeypatch, tmp_path)
    token_file = tmp_path / "tokens.json"
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(token_file))
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "private-client-secret")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1")
    auth_lifecycle.record_full_oauth_authorization(1)
    auth_health.record_auth_failure("invalid_grant")

    class TokenResponse:
        ok = True
        status_code = 200

        def json(self):
            return {"access_token": "private-access-token", "refresh_token": "private-refresh-token", "expires_in": 1800}

    class QuoteResponse:
        ok = True
        status_code = 200

        def json(self):
            return {"$SPX": {"quote": {"symbol": "$SPX"}}}

    monkeypatch.setattr(schwab.requests, "post", lambda *a, **k: TokenResponse())
    monkeypatch.setattr(schwab.requests, "get", lambda *a, **k: QuoteResponse())
    client = app_module.app.test_client()
    assert login(client).status_code == 303
    launch = client.get("/operator/reauthorize-schwab")
    state = parse_qs(urlparse(launch.headers["Location"]).query)["state"][0]
    csrf = csrf_from(client.get("/operator/login"))
    code = "private-hosted-authorization-code"

    response = client.post("/operator/complete-schwab-reauthorization", data={
        "csrf_token": csrf,
        "authorization_response": f"https://127.0.0.1/?code={code}&state={state}&session=provider-session",
    })

    assert response.status_code == 303
    assert response.headers["Location"] == "/"
    assert token_file.is_file()
    lifecycle = auth_lifecycle.load_lifecycle()
    assert set(lifecycle) == {"last_full_oauth_at", "next_reauth_due_at"}
    assert lifecycle["last_full_oauth_at"] != "1970-01-01T00:00:01+00:00"
    status = client.get("/api/operator/auth-status")
    assert status.json["reauthorization_pending"] is False
    assert status.json["reauth_required"] is False
    assert auth_health.load_status()["health"] == "healthy"
    success_page = client.get("/").get_data(as_text=True)
    assert "Schwab Connected ✓" in success_page
    assert "Authorization refreshed. Schwab authentication was validated." in success_page
    assert 'id="operator-reauth-completion"' not in success_page
    assert code not in caplog.text
    assert "private-client-secret" not in caplog.text
    assert "private-access-token" not in caplog.text
    assert "private-refresh-token" not in caplog.text


def test_hosted_invalid_grant_error_is_sanitized(monkeypatch, tmp_path, caplog):
    configure_operator(monkeypatch, tmp_path)
    install_hosted_template(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "private-client-secret")

    class RejectedResponse:
        ok = False
        status_code = 400
        text = "provider body with private details"

        def json(self):
            return {"error": "invalid_grant", "error_description": "private grant details"}

    monkeypatch.setattr(schwab.requests, "post", lambda *a, **k: RejectedResponse())
    client = app_module.app.test_client()
    assert login(client).status_code == 303
    launch = client.get("/operator/reauthorize-schwab")
    state = parse_qs(urlparse(launch.headers["Location"]).query)["state"][0]
    csrf = csrf_from(client.get("/operator/login"))
    code = "private-invalid-grant-code"

    response = client.post("/operator/complete-schwab-reauthorization", data={
        "csrf_token": csrf,
        "authorization_response": f"https://127.0.0.1/?code={code}&state={state}",
    })

    assert response.status_code == 303
    assert code not in caplog.text
    assert "private-client-secret" not in caplog.text
    assert "private grant details" not in caplog.text
    assert "provider body with private details" not in caplog.text
    assert "code_rejected" in caplog.text
    assert client.get("/api/operator/auth-status").json["reauthorization_pending"] is True
    error_page = client.get("/").get_data(as_text=True)
    assert "Schwab rejected this authorization code." in error_page
    assert code not in error_page
    assert "private grant details" not in error_page


@pytest.mark.parametrize(
    ("failure_class", "expected_message"),
    [
        ("invalid_client", "Schwab rejected the configured Client ID or Client Secret."),
        ("local_save_error", "Schwab authorization could not be saved locally."),
    ],
)
def test_hosted_reauthorization_failure_classes_render_only_safe_messages(
    monkeypatch, tmp_path, caplog, failure_class, expected_message
):
    configure_operator(monkeypatch, tmp_path)
    install_hosted_template(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "private-client-secret")

    def fail_exchange(*args, **kwargs):
        raise schwab.SchwabError("provider response with private content", failure_class=failure_class)

    monkeypatch.setattr(schwab, "exchange_authorization_response", fail_exchange)
    client = app_module.app.test_client()
    assert login(client).status_code == 303
    launch = client.get("/operator/reauthorize-schwab")
    state = parse_qs(urlparse(launch.headers["Location"]).query)["state"][0]
    csrf = csrf_from(client.get("/operator/login"))
    code = "never-render-this-code"

    response = client.post("/operator/complete-schwab-reauthorization", data={
        "csrf_token": csrf,
        "authorization_response": f"https://127.0.0.1/?code={code}&state={state}",
    })

    assert response.status_code == 303
    page = client.get("/").get_data(as_text=True)
    assert expected_message in page
    assert "provider response with private content" not in page
    assert "private-client-secret" not in page
    assert code not in page
    assert "provider response with private content" not in caplog.text
    assert "private-client-secret" not in caplog.text
    assert code not in caplog.text


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
