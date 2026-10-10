import importlib
import os
import re
from urllib.parse import parse_qs, urlparse

import pytest

from gex_client import auth_health, auth_lifecycle, local_setup, schwab


app_module = importlib.import_module("gex_client.app")


def configure_local_setup(monkeypatch, tmp_path):
    for key in (
        "SCHWAB_CLIENT_ID", "SCHWAB_CLIENT_SECRET", "SCHWAB_REDIRECT_URI",
        "GEX_OPERATOR_USERNAME", "GEX_OPERATOR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SCHWAB_CREDENTIALS_PATH", str(tmp_path / "config" / "credentials.json"))
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "config" / "tokens.json"))
    monkeypatch.setenv("SCHWAB_AUTH_HEALTH_PATH", str(tmp_path / "config" / "health.json"))
    monkeypatch.setenv("SCHWAB_AUTH_LIFECYCLE_PATH", str(tmp_path / "config" / "lifecycle.json"))
    monkeypatch.setitem(app_module.app.config, "SECRET_KEY", "local-session-secret-" + "x" * 32)
    monkeypatch.setitem(app_module.app.config, "LOCAL_SETUP_ENABLED", True)
    monkeypatch.setitem(app_module.app.config, "SESSION_COOKIE_SECURE", False)


def csrf_from(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match
    return match.group(1)


def test_first_run_redirects_to_gui_setup_without_environment_credentials(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    client = app_module.app.test_client()

    response = client.get("/")
    setup = client.get("/setup")

    assert response.status_code == 303
    assert response.headers["Location"] == "/setup"
    assert setup.status_code == 200
    body = setup.get_data(as_text=True)
    assert 'name="client_id"' in body
    assert 'name="client_secret"' in body
    assert 'name="redirect_uri"' in body
    assert 'value="https://127.0.0.1"' in body
    assert '<h1 id="setup-title">Connect your Schwab developer app</h1>' in body
    assert "Schwab Client ID" in body
    assert "Schwab Client Secret" in body
    assert "Copy the Client ID and Client Secret from your Schwab Developer Portal app details." in body
    assert "Schwab App Key" not in body
    assert "Schwab App Secret" not in body
    assert "Save Schwab credentials" in body
    assert "SCHWAB_CLIENT_ID" not in body


def test_save_credentials_returns_to_setup_before_explicit_authorization_launch(
    monkeypatch, tmp_path, caplog
):
    configure_local_setup(monkeypatch, tmp_path)
    client = app_module.app.test_client()
    page = client.get("/setup")
    secret = "private-test-app-secret"

    response = client.post("/setup/credentials", data={
        "csrf_token": csrf_from(page),
        "client_id": "private-test-app-key",
        "client_secret": secret,
        "redirect_uri": "https://127.0.0.1",
    })

    credentials_path = tmp_path / "config" / "credentials.json"
    assert response.status_code == 303
    assert response.headers["Location"] == "/setup"
    assert "api.schwabapi.com" not in response.headers["Location"]
    setup_after_save = client.get(response.headers["Location"])
    assert setup_after_save.status_code == 200
    body = setup_after_save.get_data(as_text=True)
    assert '<h1 id="setup-title">Connect your Schwab developer app</h1>' in body
    assert "Step 2" in body
    assert "Authorize with Schwab" in body
    assert "Schwab will open in a new browser tab." in body
    assert "This is expected." in body
    assert "Step 3" in body
    assert 'placeholder="https://127.0.0.1/?code=..."' in body
    assert 'href="/operator/reauthorize-schwab" target="_blank" rel="noopener noreferrer"' in body
    assert "Open Schwab Authorization" in body
    assert "Complete Schwab Connection" in body
    assert secret not in body

    authorize = client.get("/operator/reauthorize-schwab")
    assert authorize.status_code == 302
    assert setup_after_save.status_code == 200
    authorize_params = parse_qs(urlparse(authorize.headers["Location"]).query)
    assert authorize_params["client_id"] == ["private-test-app-key"]
    assert authorize_params["redirect_uri"] == ["https://127.0.0.1"]
    oauth_state = authorize_params["state"][0]
    assert len(oauth_state) >= 32
    assert secret not in response.get_data(as_text=True)
    assert secret not in response.headers["Location"]
    assert secret not in caplog.text
    if os.name == "posix":
        assert credentials_path.stat().st_mode & 0o777 == 0o600
    assert secret in credentials_path.read_text(encoding="utf-8")
    assert "Restart Schwab Authorization" in client.get("/setup").get_data(as_text=True)


def test_restarting_authorization_replaces_pending_state(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    local_setup.save_credentials("client-id", "client-secret", "https://127.0.0.1")
    client = app_module.app.test_client()

    first = client.get("/operator/reauthorize-schwab")
    first_state = parse_qs(urlparse(first.headers["Location"]).query)["state"][0]
    second = client.get("/operator/reauthorize-schwab")
    second_state = parse_qs(urlparse(second.headers["Location"]).query)["state"][0]

    assert first_state != second_state
    page = client.get("/setup")
    stale = client.post("/setup/complete", data={
        "csrf_token": csrf_from(page),
        "authorization_response": f"https://127.0.0.1/?code=old-code&state={first_state}",
    })

    assert stale.status_code == 400


def test_save_connect_rejects_non_loopback_and_missing_csrf(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    client = app_module.app.test_client()
    remote = client.get("/setup", environ_base={"REMOTE_ADDR": "198.51.100.20"})
    assert remote.status_code == 404

    page = client.get("/setup")
    response = client.post("/setup/credentials", data={
        "client_id": "app-key",
        "client_secret": "app-secret",
        "redirect_uri": "https://127.0.0.1",
    })
    assert page.status_code == 200
    assert response.status_code == 400
    assert not (tmp_path / "config" / "credentials.json").exists()


def test_setup_does_not_replace_saved_credentials_without_a_separate_flow(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    local_setup.save_credentials("existing-app-key", "existing-app-secret", "https://127.0.0.1")
    client = app_module.app.test_client()
    page = client.get("/setup")

    response = client.post("/setup/credentials", data={
        "csrf_token": csrf_from(page),
        "client_id": "replacement-app-key",
        "client_secret": "replacement-app-secret",
        "redirect_uri": "https://127.0.0.1",
    })

    assert page.status_code == 200
    assert response.status_code == 409
    assert local_setup.load_credentials() == {
        "client_id": "existing-app-key",
        "client_secret": "existing-app-secret",
        "redirect_uri": "https://127.0.0.1",
    }
    assert "existing-app-secret" not in response.get_data(as_text=True)
    assert "replacement-app-secret" not in response.get_data(as_text=True)


def test_local_setup_rejects_loopback_proxy_with_non_loopback_host(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    client = app_module.app.test_client()

    response = client.get(
        "/setup",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Host": "gex.example.com"},
    )

    assert response.status_code == 404


def test_manual_environment_setup_remains_supported(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "advanced-key")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "advanced-secret")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1")
    monkeypatch.setattr(schwab, "authorization_url", lambda: "https://api.schwabapi.com/authorize")
    client = app_module.app.test_client()

    response = client.get("/setup")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Credentials are available from your environment" in body
    assert "advanced-key" not in body
    assert "advanced-secret" not in body
    assert 'name="client_secret"' not in body


def test_setup_page_never_renders_stored_key_or_secret(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    local_setup.save_credentials("private-app-key", "private-app-secret", "https://127.0.0.1")
    client = app_module.app.test_client()

    response = client.get("/setup")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "private-app-key" not in body
    assert "private-app-secret" not in body
    assert "value=\"https://127.0.0.1\"" not in body
    assert "localStorage" not in body


def test_callback_uses_existing_exchange_and_redirects_to_connected_dashboard(
    monkeypatch, tmp_path, caplog
):
    configure_local_setup(monkeypatch, tmp_path)
    local_setup.save_credentials("app-key", "app-secret", "https://127.0.0.1")
    client = app_module.app.test_client()
    provider_calls = []

    class ProviderResponse:
        ok = True
        status_code = 200

        def json(self):
            return {
                "access_token": "private-access-token",
                "refresh_token": "private-refresh-token",
                "expires_in": 1800,
            }

    class QuoteResponse:
        ok = True
        status_code = 200

        def json(self):
            return {"$SPX": {"quote": {"symbol": "$SPX"}}}

    def provider_post(url, **kwargs):
        provider_calls.append((url, kwargs))
        return ProviderResponse()

    monkeypatch.setattr(schwab.requests, "post", provider_post)
    monkeypatch.setattr(schwab.requests, "get", lambda *args, **kwargs: QuoteResponse())
    authorize = client.get("/operator/reauthorize-schwab")
    expected_state = parse_qs(urlparse(authorize.headers["Location"]).query)["state"][0]
    page = client.get("/setup")
    callback = f"https://127.0.0.1/?code=one-time-auth-code&state={expected_state}"

    response = client.post("/setup/complete", data={
        "csrf_token": csrf_from(page),
        "authorization_response": callback,
    })
    dashboard = client.get(response.headers["Location"])
    setup_status = client.get("/api/setup-status")

    assert len(provider_calls) == 1
    assert provider_calls[0][1]["data"] == {
        "grant_type": "authorization_code",
        "code": "one-time-auth-code",
        "redirect_uri": "https://127.0.0.1",
    }
    assert response.status_code == 303
    assert "code=" not in response.headers["Location"]
    assert dashboard.status_code == 200
    assert setup_status.status_code == 200
    assert setup_status.json == {
        "ready": True,
        "client_id_configured": True,
        "client_secret_configured": True,
        "token_configured": True,
        "authorization_pending": False,
    }
    assert set(auth_lifecycle.load_lifecycle()) == {"last_full_oauth_at", "next_reauth_due_at"}
    dashboard_body = dashboard.get_data(as_text=True)
    assert 'id="setup-card" hidden' in dashboard_body
    assert "Complete the local Schwab setup" not in dashboard_body
    assert "http://127.0.0.1:8765" not in dashboard_body
    assert "Schwab Connected" in dashboard.get_data(as_text=True)
    assert "Schwab Connected ✓" in dashboard.get_data(as_text=True)
    assert "Authorization complete. Foxchase GEX can now retrieve SPX and NDX market data." in dashboard.get_data(as_text=True)
    assert callback not in dashboard.get_data(as_text=True)
    assert "one-time-auth-code" not in caplog.text
    assert "private-access-token" not in dashboard_body
    assert "private-refresh-token" not in dashboard_body

    replay = client.post("/setup/complete", data={
        "csrf_token": csrf_from(page),
        "authorization_response": callback,
    })
    assert replay.status_code == 409
    assert len(provider_calls) == 1


def test_callback_failure_is_sanitized_and_does_not_log_code(monkeypatch, tmp_path, caplog):
    configure_local_setup(monkeypatch, tmp_path)
    local_setup.save_credentials("app-key", "app-secret", "https://127.0.0.1")
    client = app_module.app.test_client()
    code = "never-log-this-auth-code"

    def exchange(_value, expected_state=None):
        raise schwab.SchwabError(f"provider error included {code}")

    monkeypatch.setattr(schwab, "exchange_authorization_response", exchange)
    authorize = client.get("/operator/reauthorize-schwab")
    expected_state = parse_qs(urlparse(authorize.headers["Location"]).query)["state"][0]
    page = client.get("/setup")
    response = client.post("/setup/complete", data={
        "csrf_token": csrf_from(page),
        "authorization_response": f"https://127.0.0.1/?code={code}&state={expected_state}",
    })

    assert response.status_code == 400
    assert code not in response.get_data(as_text=True)
    assert code not in caplog.text
    assert "Could not complete Schwab authorization" in response.get_data(as_text=True)


@pytest.mark.parametrize(
    ("provider_error", "expected_message"),
    [
        (
            "invalid_grant",
            "Schwab rejected this authorization code. Start a new authorization and paste the newest redirected URL.",
        ),
        (
            "invalid_client",
            "Schwab rejected the Client ID or Client Secret. Verify them in the Schwab Developer Portal.",
        ),
    ],
)
def test_oauth_provider_failures_have_safe_specific_messages(
    monkeypatch, tmp_path, caplog, provider_error, expected_message
):
    configure_local_setup(monkeypatch, tmp_path)
    secret = "private-client-secret"
    local_setup.save_credentials("private-client-id", secret, "https://127.0.0.1")

    class ProviderResponse:
        ok = False
        status_code = 400
        text = "provider response with sensitive details"

        def json(self):
            return {
                "error": provider_error,
                "error_description": "private provider description",
            }

    monkeypatch.setattr(schwab.requests, "post", lambda *args, **kwargs: ProviderResponse())
    client = app_module.app.test_client()
    authorize = client.get("/operator/reauthorize-schwab")
    state = parse_qs(urlparse(authorize.headers["Location"]).query)["state"][0]
    page = client.get("/setup")
    code = "private-authorization-code"
    response = client.post("/setup/complete", data={
        "csrf_token": csrf_from(page),
        "authorization_response": f"https://127.0.0.1/?code={code}&state={state}",
    })
    body = response.get_data(as_text=True)

    assert response.status_code == 400
    assert expected_message in body
    assert code not in body + caplog.text
    assert secret not in body + caplog.text
    assert "private provider description" not in body + caplog.text
    assert "provider response with sensitive details" not in body + caplog.text


def test_successful_oauth_with_local_save_failure_has_specific_safe_message(
    monkeypatch, tmp_path, caplog
):
    configure_local_setup(monkeypatch, tmp_path)
    secret = "private-client-secret"
    local_setup.save_credentials("private-client-id", secret, "https://127.0.0.1")

    class ProviderResponse:
        ok = True
        status_code = 200

        def json(self):
            return {"access_token": "private-access-token", "refresh_token": "private-refresh-token"}

    monkeypatch.setattr(schwab.requests, "post", lambda *args, **kwargs: ProviderResponse())

    def fail_token_save(_tokens):
        raise OSError("private filesystem details")

    monkeypatch.setattr(schwab, "save_tokens", fail_token_save)
    client = app_module.app.test_client()
    authorize = client.get("/operator/reauthorize-schwab")
    state = parse_qs(urlparse(authorize.headers["Location"]).query)["state"][0]
    page = client.get("/setup")
    code = "private-authorization-code"
    response = client.post("/setup/complete", data={
        "csrf_token": csrf_from(page),
        "authorization_response": f"https://127.0.0.1/?code={code}&state={state}",
    })
    body = response.get_data(as_text=True)

    assert response.status_code == 400
    assert "Schwab authorization succeeded, but Foxchase GEX could not save the local authorization state." in body
    assert all(value not in body + caplog.text for value in (
        code, secret, "private-access-token", "private-refresh-token", "private filesystem details"
    ))


def test_callback_state_mismatch_is_rejected_before_token_exchange(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    local_setup.save_credentials("app-key", "app-secret", "https://127.0.0.1")
    client = app_module.app.test_client()
    exchange_calls = []
    monkeypatch.setattr(
        schwab.requests,
        "post",
        lambda *args, **kwargs: exchange_calls.append((args, kwargs)),
    )
    authorize = client.get("/operator/reauthorize-schwab")
    expected_state = parse_qs(urlparse(authorize.headers["Location"]).query)["state"][0]
    page = client.get("/setup")
    response = client.post("/setup/complete", data={
        "csrf_token": csrf_from(page),
        "authorization_response": "https://127.0.0.1/?code=foreign-code&state=wrong-state",
    })

    assert expected_state != "wrong-state"
    assert response.status_code == 400
    assert exchange_calls == []
    assert "This authorization no longer matches the current setup session." in response.get_data(as_text=True)
    assert "Restart Schwab Authorization" in response.get_data(as_text=True)


def test_local_reauthorization_keeps_callback_form_reachable_for_connected_user(
    monkeypatch, tmp_path
):
    configure_local_setup(monkeypatch, tmp_path)
    local_setup.save_credentials("app-key", "app-secret", "https://127.0.0.1")
    token_path = tmp_path / "config" / "tokens.json"
    token_path.write_text('{"access_token":"redacted"}', encoding="utf-8")
    monkeypatch.setattr(app_module, "token_path", lambda: token_path)
    client = app_module.app.test_client()

    assert client.get("/setup").status_code == 303
    authorize = client.get("/operator/reauthorize-schwab")
    state = parse_qs(urlparse(authorize.headers["Location"]).query)["state"][0]
    setup = client.get("/setup")
    status = client.get("/api/setup-status")
    dashboard = client.get("/")

    assert setup.status_code == 200
    setup_body = setup.get_data(as_text=True)
    assert '<h1 id="setup-title">Reauthorize Schwab</h1>' in setup_body
    assert 'name="authorization_response"' in setup_body
    assert status.json["authorization_pending"] is True
    assert state not in status.get_data(as_text=True)
    assert 'id="local-auth-continue"' in dashboard.get_data(as_text=True)


def test_missing_token_with_invalid_grant_latch_uses_reauthorization_heading(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    local_setup.save_credentials("existing-client-id", "existing-client-secret", "https://127.0.0.1")
    auth_health.record_auth_failure("invalid_grant")
    client = app_module.app.test_client()

    authorize = client.get("/operator/reauthorize-schwab")
    assert authorize.status_code == 302
    setup = client.get("/setup")

    assert setup.status_code == 200
    body = setup.get_data(as_text=True)
    assert '<h1 id="setup-title">Reauthorize Schwab</h1>' in body
    assert "Connect your Schwab developer app" not in body


def test_setup_routes_and_local_operator_status_are_disabled_outside_local_mode(
    monkeypatch, tmp_path
):
    configure_local_setup(monkeypatch, tmp_path)
    monkeypatch.setitem(app_module.app.config, "LOCAL_SETUP_ENABLED", False)
    monkeypatch.setenv("GEX_OPERATOR_USERNAME", "operator")
    monkeypatch.setenv("GEX_OPERATOR_PASSWORD_HASH", "scrypt:32768:8:1$bad$bad")
    client = app_module.app.test_client()

    assert client.get("/setup").status_code == 404
    assert client.post("/setup/credentials").status_code == 404
    assert client.get("/api/operator/auth-status").status_code == 401
