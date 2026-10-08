import json

import pytest

from gex_client import local_setup


def test_credentials_are_stored_atomically_with_private_permissions(monkeypatch, tmp_path):
    path = tmp_path / "private" / "schwab_credentials.json"
    monkeypatch.setenv("SCHWAB_CREDENTIALS_PATH", str(path))
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    monkeypatch.delenv("SCHWAB_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SCHWAB_REDIRECT_URI", raising=False)

    local_setup.save_credentials("app-key", "app-secret", "https://127.0.0.1")

    assert local_setup.load_credentials() == {
        "client_id": "app-key",
        "client_secret": "app-secret",
        "redirect_uri": "https://127.0.0.1",
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert set(json.loads(path.read_text())) == {"client_id", "client_secret", "redirect_uri"}


def test_environment_credentials_take_precedence_over_local_file(monkeypatch, tmp_path):
    path = tmp_path / "schwab_credentials.json"
    monkeypatch.setenv("SCHWAB_CREDENTIALS_PATH", str(path))
    local_setup.save_credentials("file-key", "file-secret", "https://127.0.0.1/file")
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "environment-key")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "environment-secret")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/env")

    assert local_setup.configured_credentials() == (
        "environment-key", "environment-secret", "https://127.0.0.1/env"
    )
    assert local_setup.credential_status()["source"] == "environment"


def test_credentials_loader_rejects_a_relaxed_parent_directory(monkeypatch, tmp_path):
    path = tmp_path / "private" / "schwab_credentials.json"
    monkeypatch.setenv("SCHWAB_CREDENTIALS_PATH", str(path))
    local_setup.save_credentials("app-key", "app-secret", "https://127.0.0.1")
    path.parent.chmod(0o755)

    with pytest.raises(local_setup.CredentialStoreError, match="mode 0700"):
        local_setup.load_credentials()


def test_partial_environment_credentials_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHWAB_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "environment-key")
    monkeypatch.delenv("SCHWAB_CLIENT_SECRET", raising=False)

    assert local_setup.credential_status()["source"] == "incomplete_environment"
    with pytest.raises(local_setup.CredentialStoreError, match="both"):
        local_setup.configured_credentials()


def test_session_secret_is_created_once_with_private_permissions(monkeypatch, tmp_path):
    path = tmp_path / "private" / "session_secret"
    monkeypatch.setenv("GEX_SESSION_SECRET_PATH", str(path))

    first = local_setup.get_or_create_session_secret()
    second = local_setup.get_or_create_session_secret()

    assert len(first) >= 32
    assert second == first
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "redirect_uri",
    ["", "not-a-url", "http://127.0.0.1", "https://user:pass@example.com/callback"],
)
def test_unsafe_or_invalid_redirect_uri_rejected(monkeypatch, tmp_path, redirect_uri):
    monkeypatch.setenv("SCHWAB_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))

    with pytest.raises(local_setup.CredentialStoreError):
        local_setup.save_credentials("app-key", "app-secret", redirect_uri)
