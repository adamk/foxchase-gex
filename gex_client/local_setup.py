"""Private local Schwab app-credential storage for first-run GUI setup."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_REDIRECT_URI = "https://127.0.0.1"
_CREDENTIAL_FIELDS = {"client_id", "client_secret", "redirect_uri"}


class CredentialStoreError(ValueError):
    """Sanitized local setup or credential-store failure."""


def _config_dir() -> Path:
    return Path(os.getenv("FOXCHASE_GEX_CONFIG_DIR", "~/.foxchase-gex")).expanduser()


def credentials_path() -> Path:
    configured = os.getenv("SCHWAB_CREDENTIALS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    token_path = os.getenv("SCHWAB_TOKEN_PATH", "").strip()
    if token_path:
        return Path(token_path).expanduser().with_name("schwab_credentials.json")
    return _config_dir() / "schwab_credentials.json"


def session_secret_path() -> Path:
    configured = os.getenv("GEX_SESSION_SECRET_PATH", "").strip()
    return Path(configured).expanduser() if configured else _config_dir() / "session_secret"


def _ensure_private_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.parent.stat()
    except OSError as exc:
        raise CredentialStoreError("local configuration directory is unavailable") from exc
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise CredentialStoreError("local configuration directory must be owned by this user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise CredentialStoreError("local configuration directory must have mode 0700")


def _validate_redirect_uri(value: str) -> str:
    redirect_uri = value.strip()
    try:
        parsed = urlsplit(redirect_uri)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid or len(redirect_uri) > 2048:
        raise CredentialStoreError("redirect URI must be a valid HTTPS callback URL")
    return redirect_uri


def _validate_credentials(client_id: str, client_secret: str, redirect_uri: str) -> dict:
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise CredentialStoreError("app key and secret are required")
    app_key = client_id.strip()
    app_secret = client_secret.strip()
    if (
        not app_key
        or not app_secret
        or len(app_key) > 1024
        or len(app_secret) > 4096
        or any(ord(char) < 32 for char in app_key + app_secret)
    ):
        raise CredentialStoreError("app key and secret are required")
    if not isinstance(redirect_uri, str):
        raise CredentialStoreError("redirect URI is required")
    return {
        "client_id": app_key,
        "client_secret": app_secret,
        "redirect_uri": _validate_redirect_uri(redirect_uri),
    }


def load_credentials() -> dict | None:
    path = credentials_path()
    _ensure_private_parent(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialStoreError("local credential file is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CredentialStoreError("local credential file must be a regular file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise CredentialStoreError("local credential file must be owned by this user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise CredentialStoreError("local credential file must have mode 0600")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialStoreError("local credential file is invalid") from exc
    if not isinstance(value, dict) or set(value) != _CREDENTIAL_FIELDS:
        raise CredentialStoreError("local credential file is invalid")
    return _validate_credentials(
        value.get("client_id"), value.get("client_secret"), value.get("redirect_uri")
    )


def save_credentials(client_id: str, client_secret: str, redirect_uri: str) -> dict:
    value = _validate_credentials(client_id, client_secret, redirect_uri)
    path = credentials_path()
    _ensure_private_parent(path)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise CredentialStoreError("local credential file is unavailable") from exc
    if existing and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
        raise CredentialStoreError("local credential file must be a regular file")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=".schwab-credentials-", text=True
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise CredentialStoreError("local credentials could not be saved securely") from exc
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return {"client_id": value["client_id"], "redirect_uri": value["redirect_uri"]}


def credential_status() -> dict:
    env_client_id = os.getenv("SCHWAB_CLIENT_ID", "").strip()
    env_client_secret = os.getenv("SCHWAB_CLIENT_SECRET", "").strip()
    if env_client_id or env_client_secret:
        return {
            "source": "environment" if env_client_id and env_client_secret else "incomplete_environment",
            "client_id_configured": bool(env_client_id),
            "client_secret_configured": bool(env_client_secret),
        }
    try:
        configured = load_credentials()
    except CredentialStoreError:
        return {
            "source": "invalid_local_file",
            "client_id_configured": False,
            "client_secret_configured": False,
        }
    return {
        "source": "local_file" if configured else "missing",
        "client_id_configured": configured is not None,
        "client_secret_configured": configured is not None,
    }


def configured_credentials() -> tuple[str, str, str]:
    env_client_id = os.getenv("SCHWAB_CLIENT_ID", "").strip()
    env_client_secret = os.getenv("SCHWAB_CLIENT_SECRET", "").strip()
    if env_client_id or env_client_secret:
        if not env_client_id or not env_client_secret:
            raise CredentialStoreError(
                "both SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET must be configured"
            )
        redirect_uri = os.getenv("SCHWAB_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI
        return env_client_id, env_client_secret, redirect_uri
    value = load_credentials()
    if value is None:
        raise CredentialStoreError(
            "Schwab credentials are missing; use the local setup page or configure both environment variables"
        )
    return value["client_id"], value["client_secret"], value["redirect_uri"]


def get_or_create_session_secret() -> str:
    path = session_secret_path()
    _ensure_private_parent(path)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise CredentialStoreError("local session secret must be a private mode-0600 file")
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CredentialStoreError("local session secret is unavailable") from exc
        if len(value) < 32:
            raise CredentialStoreError("local session secret is invalid")
        return value
    except OSError as exc:
        raise CredentialStoreError("local session secret could not be created") from exc

    value = secrets.token_urlsafe(48)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise CredentialStoreError("local session secret could not be created") from exc
    return value
