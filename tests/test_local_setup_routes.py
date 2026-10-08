import importlib
import re
from urllib.parse import parse_qs, urlparse

import pytest

from gex_client import local_setup, schwab


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
    assert "Save &amp; Connect Schwab" in body
    assert "SCHWAB_CLIENT_ID" not in body


def test_save_connect_stores_locally_and_redirects_using_existing_authorization_url(
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
    assert urlparse(response.headers["Location"]).hostname == "api.schwabapi.com"
    authorize_params = parse_qs(urlparse(response.headers["Location"]).query)
    assert authorize_params["client_id"] == ["private-test-app-key"]
    assert authorize_params["redirect_uri"] == ["https://127.0.0.1"]
    oauth_state = authorize_params["state"][0]
    assert len(oauth_state) >= 32
    assert secret not in response.get_data(as_text=True)
    assert secret not in response.headers["Location"]
    assert secret not in caplog.text
    assert credentials_path.stat().st_mode & 0o777 == 0o600
    assert secret in credentials_path.read_text(encoding="utf-8")


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
    seen = []

    def exchange(value, expected_state=None):
        seen.append((value, expected_state))
        (tmp_path / "config" / "tokens.json").write_text('{"access_token":"redacted"}')
        return {"ok": True}

    monkeypatch.setattr(schwab, "exchange_authorization_response", exchange)
    authorize = client.get("/operator/reauthorize-schwab")
    expected_state = parse_qs(urlparse(authorize.headers["Location"]).query)["state"][0]
    page = client.get("/setup")
    callback = f"https://127.0.0.1/?code=one-time-auth-code&state={expected_state}"

    response = client.post("/setup/complete", data={
        "csrf_token": csrf_from(page),
        "authorization_response": callback,
    })
    dashboard = client.get(response.headers["Location"])

    assert seen == [(callback, expected_state)]
    assert response.status_code == 303
    assert "code=" not in response.headers["Location"]
    assert dashboard.status_code == 200
    assert "Schwab Connected" in dashboard.get_data(as_text=True)
    assert callback not in dashboard.get_data(as_text=True)
    assert "one-time-auth-code" not in caplog.text

    replay = client.post("/setup/complete", data={
        "csrf_token": csrf_from(page),
        "authorization_response": callback,
    })
    assert replay.status_code == 409
    assert len(seen) == 1


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
    assert 'name="authorization_response"' in setup.get_data(as_text=True)
    assert status.json["authorization_pending"] is True
    assert state not in status.get_data(as_text=True)
    assert 'id="local-auth-continue"' in dashboard.get_data(as_text=True)


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
