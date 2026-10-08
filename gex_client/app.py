from __future__ import annotations

import os
import hmac
import ipaddress
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, session
from werkzeug.security import check_password_hash

from gex_client import auth_lifecycle
from gex_client import local_setup, schwab
from gex_client.archive import list_sessions, snapshot, timeline
from gex_client.schwab import (
    SchwabError,
    fetch_sanitized_snapshot,
    token_path,
)


load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
runtime_mode = os.getenv("GEX_ENV", "production").strip().lower()
local_mode = runtime_mode in {"development", "local", "test"}
local_setup_enabled = local_mode and os.getenv("GEX_LOCAL_SETUP_ENABLED", "") == "1"
session_secret = os.getenv("GEX_SESSION_SECRET") or None
if session_secret is None and local_setup_enabled:
    try:
        session_secret = local_setup.get_or_create_session_secret()
    except local_setup.CredentialStoreError:
        session_secret = None
app.config.update(
    SECRET_KEY=session_secret,
    SESSION_COOKIE_NAME="gex_operator_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=not local_mode,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    LOCAL_SETUP_ENABLED=local_setup_enabled,
    FOXCHASE_GEX_API_URL=os.getenv(
        "FOXCHASE_GEX_API_URL", "https://compute.foxchasetrading.com/api/community"
    ).rstrip("/"),
    FOXCHASE_GEX_PORT=int(os.getenv("FOXCHASE_GEX_PORT", "8765")),
    MAX_CONTENT_LENGTH=16_384,
)

_RESULT_CACHE: dict[str, dict] = {}
_RESULT_CACHE_SECONDS = 20


def _session_id() -> str:
    value = request.headers.get("X-GEX-Session", "").strip()
    if not value or len(value) > 128:
        raise ValueError("X-GEX-Session is required")
    return value


def _remote_json(method: str, path: str, session_id: str, payload=None):
    try:
        response = requests.request(
            method,
            f"{app.config['FOXCHASE_GEX_API_URL']}/{path.lstrip('/')}",
            headers={"X-GEX-Session": session_id, "Accept": "application/json"},
            json=payload,
            timeout=40,
        )
        body = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Foxchase calculation service is unavailable: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("Foxchase calculation service returned an invalid response") from exc
    return response.status_code, body


@app.after_request
def no_store_api(response):
    if (
        request.path.startswith("/api/")
        or request.path.startswith("/operator/")
        or request.path.startswith("/setup")
    ):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _operator_auth_ready() -> bool:
    username = os.getenv("GEX_OPERATOR_USERNAME", "").strip()
    password_hash = os.getenv("GEX_OPERATOR_PASSWORD_HASH", "").strip()
    secret = app.config.get("SECRET_KEY")
    return bool(
        username
        and password_hash
        and isinstance(secret, str)
        and len(secret.encode("utf-8")) >= 32
    )


def _operator_gate():
    if _local_setup_request():
        return None
    if not _operator_auth_ready():
        return jsonify({"error": "operator authentication is not configured"}), 503
    if session.get("operator_authenticated") is not True:
        return jsonify({"error": "operator login required"}), 401
    return None


def _csrf_token() -> str:
    token = session.get("operator_csrf_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session["operator_csrf_token"] = token
    return token


def _valid_csrf() -> bool:
    expected = session.get("operator_csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not isinstance(expected, str) or not isinstance(supplied, str) or not expected:
        return False
    try:
        return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
    except UnicodeError:
        return False


def _local_setup_request() -> bool:
    if not app.config.get("LOCAL_SETUP_ENABLED"):
        return False
    try:
        remote_is_loopback = ipaddress.ip_address(request.remote_addr or "").is_loopback
        host = urlsplit(f"//{request.host}").hostname or ""
        host_is_loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        return remote_is_loopback and host_is_loopback
    except ValueError:
        return False


def _setup_status_payload() -> dict:
    configured = local_setup.credential_status()
    client_id = bool(configured["client_id_configured"])
    client_secret = bool(configured["client_secret_configured"])
    token = token_path().is_file()
    oauth_state = session.get("local_schwab_oauth_state")
    return {
        "ready": client_id and client_secret and token,
        "client_id_configured": client_id,
        "client_secret_configured": client_secret,
        "token_configured": token,
        "authorization_pending": isinstance(oauth_state, str) and len(oauth_state) >= 32,
    }


def _render_setup(error_message: str | None = None, status_code: int = 200):
    if not isinstance(app.config.get("SECRET_KEY"), str) or len(app.config["SECRET_KEY"]) < 32:
        return "Local setup session key is unavailable; no credentials were saved.", 503
    configured = local_setup.credential_status()
    setup_status = _setup_status_payload()
    response = make_response(
        render_template(
            "setup.html",
            csrf_token=_csrf_token(),
            credential_source=configured["source"],
            setup_ready=setup_status["ready"],
            authorization_pending=setup_status["authorization_pending"],
            credentials_configured=(
                configured["client_id_configured"]
                and configured["client_secret_configured"]
            ),
            error_message=error_message,
        ),
        status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _authorization_redirect(code: int = 302):
    if _local_setup_request():
        state = secrets.token_urlsafe(32)
        session["local_schwab_oauth_state"] = state
        try:
            return redirect(schwab.authorization_url(state=state), code=code)
        except Exception:
            session.pop("local_schwab_oauth_state", None)
            raise
    return redirect(schwab.authorization_url(), code=code)


@app.get("/")
def dashboard():
    if _local_setup_request() and not _setup_status_payload()["ready"]:
        return redirect("/setup", code=303)
    connected_notice = False
    if isinstance(app.config.get("SECRET_KEY"), str) and len(app.config["SECRET_KEY"]) >= 32:
        connected_notice = bool(session.pop("schwab_connected_notice", False))
    return render_template(
        "index.html",
        local_operator_access=_local_setup_request(),
        schwab_connected=connected_notice,
    )


@app.get("/setup")
def local_setup_page():
    if not _local_setup_request():
        abort(404)
    setup_status = _setup_status_payload()
    if setup_status["ready"] and not setup_status["authorization_pending"]:
        return redirect("/", code=303)
    return _render_setup()


@app.post("/setup/credentials")
def save_local_schwab_credentials():
    if not _local_setup_request():
        abort(404)
    if not isinstance(app.config.get("SECRET_KEY"), str) or len(app.config["SECRET_KEY"]) < 32:
        return "Local setup session key is unavailable; no credentials were saved.", 503
    if not _valid_csrf():
        return "Invalid form session. Reload the setup page and try again.", 400

    configured = local_setup.credential_status()
    if configured["source"] == "environment":
        return _render_setup("Schwab credentials are already configured in the environment.", 409)
    if configured["source"] == "incomplete_environment":
        return _render_setup(
            "The environment contains an incomplete Schwab credential pair. Configure both values or remove the partial override.",
            409,
        )
    if configured["source"] == "local_file":
        return _render_setup("Local credentials are already saved. Continue to Schwab below.", 409)

    client_id = request.form.get("client_id", "")
    client_secret = request.form.get("client_secret", "")
    redirect_uri = request.form.get("redirect_uri", local_setup.DEFAULT_REDIRECT_URI)
    try:
        local_setup.save_credentials(client_id, client_secret, redirect_uri)
    except local_setup.CredentialStoreError:
        return _render_setup(
            "Credentials could not be saved securely. Check the values and local file permissions, then try again.",
            400,
        )
    try:
        return _authorization_redirect(code=303)
    except SchwabError:
        return _render_setup(
            "Schwab authorization could not be started. Check the app key and redirect URI, then try again.",
            400,
        )


@app.post("/setup/complete")
def complete_local_schwab_setup():
    if not _local_setup_request():
        abort(404)
    if not isinstance(app.config.get("SECRET_KEY"), str) or len(app.config["SECRET_KEY"]) < 32:
        return "Local setup session key is unavailable; no authorization response was processed.", 503
    if not _valid_csrf():
        return "Invalid form session. Reload the setup page and try again.", 400
    configured = local_setup.credential_status()
    if not (configured["client_id_configured"] and configured["client_secret_configured"]):
        return _render_setup("Save valid Schwab app credentials before completing authorization.", 409)
    expected_state = session.get("local_schwab_oauth_state")
    if not isinstance(expected_state, str) or len(expected_state) < 32:
        return _render_setup("Start a new Schwab authorization before pasting its redirect URL.", 409)

    authorization_response = request.form.get("authorization_response", "")
    if not authorization_response or len(authorization_response) > 8192:
        return _render_setup("Paste the complete Schwab redirect URL and try again.", 400)
    try:
        schwab.exchange_authorization_response(
            authorization_response, expected_state=expected_state
        )
    except Exception as exc:
        # Never log provider text or the submitted callback, which contains the code.
        app.logger.warning("Local Schwab authorization exchange failed: %s", type(exc).__name__)
        return _render_setup(
            "Could not complete Schwab authorization. Check the pasted redirect and try again.",
            400,
        )
    session.pop("local_schwab_oauth_state", None)
    session["schwab_connected_notice"] = True
    return redirect("/", code=303)


@app.route("/operator/login", methods=["GET", "POST"])
def operator_login():
    if not _operator_auth_ready():
        return "Operator authentication is not configured.", 503
    if request.method == "GET":
        response = make_response(
            render_template("operator_login.html", csrf_token=_csrf_token(), error=False)
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    if not _valid_csrf():
        return "Invalid form session. Reload the login page and try again.", 400

    expected_username = os.getenv("GEX_OPERATOR_USERNAME", "").strip()
    password_hash = os.getenv("GEX_OPERATOR_PASSWORD_HASH", "").strip()
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    valid = False
    if len(username) <= 256 and len(password) <= 256 and len(password_hash) <= 2000:
        try:
            password_ok = check_password_hash(password_hash, password)
        except (ValueError, TypeError):
            password_ok = False
        try:
            username_ok = hmac.compare_digest(
                username.encode("utf-8"), expected_username.encode("utf-8")
            )
        except UnicodeError:
            username_ok = False
        valid = password_ok and username_ok
    if not valid:
        response = make_response(
            render_template("operator_login.html", csrf_token=_csrf_token(), error=True), 401
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # Flask's signed-cookie session has no server-side identifier. Clearing it
    # and issuing a newly signed authenticated session prevents fixation.
    session.clear()
    session.permanent = True
    session["operator_authenticated"] = True
    session["operator_csrf_token"] = secrets.token_urlsafe(32)
    return redirect("/", code=303)


@app.route("/operator/logout", methods=["GET", "POST"])
def operator_logout():
    denied = _operator_gate()
    if denied:
        return denied
    if request.method == "GET":
        response = make_response(
            render_template("operator_logout.html", csrf_token=_csrf_token())
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    if not _valid_csrf():
        return "Invalid form session. Reload and try again.", 400
    session.clear()
    return redirect("/operator/login", code=303)


@app.get("/api/operator/auth-status")
def operator_auth_status():
    denied = _operator_gate()
    if denied:
        return denied
    return jsonify(auth_lifecycle.operator_status())


@app.get("/operator/reauthorize-schwab")
def operator_reauthorize_schwab():
    denied = _operator_gate()
    if denied:
        return denied
    try:
        return _authorization_redirect()
    except SchwabError:
        return "Schwab reauthorization is not configured.", 503


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "foxchase-gex-local-client"})


@app.get("/api/setup-status")
def setup_status():
    """Report only whether local OAuth inputs exist; never return their values."""
    if not _local_setup_request():
        abort(404)
    return jsonify(_setup_status_payload())


@app.get("/api/gex/<symbol>")
def gex(symbol: str):
    try:
        session_id = _session_id()
        display_symbol = symbol.upper().strip()
        if display_symbol not in {"SPX", "NDX"}:
            return jsonify({"error": "supported symbols are SPX and NDX"}), 400

        now = time.time()
        cached = _RESULT_CACHE.get(display_symbol)
        if cached and now - cached["timestamp"] < _RESULT_CACHE_SECONDS:
            result = dict(cached["result"])
            result["client_cached"] = True
            result["client_cache_age_seconds"] = round(now - cached["timestamp"], 1)
            return jsonify(result)

        snapshot = fetch_sanitized_snapshot(display_symbol)
        status, result = _remote_json("POST", "gex", session_id, snapshot)
        if status >= 400:
            return jsonify(result), status
        result["client_cached"] = False
        result["client_cache_age_seconds"] = 0
        _RESULT_CACHE[display_symbol] = {"timestamp": now, "result": dict(result)}
        return jsonify(result)
    except (SchwabError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/presence", methods=["GET", "POST"])
def presence():
    try:
        session_id = _session_id()
        status, result = _remote_json(request.method, "presence", session_id)
        return jsonify(result), status
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502


@app.get("/api/history/<symbol>/sessions")
def history_sessions(symbol: str):
    try:
        return jsonify({"symbol": symbol.upper(), "sessions": list_sessions(symbol)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/history/<symbol>/<day>/timeline")
def history_timeline(symbol: str, day: str):
    try:
        points = timeline(symbol, day)
        if not points:
            return jsonify({"error": "historical session not found"}), 404
        return jsonify({"symbol": symbol.upper(), "date": day, "timeline": points})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/history/<symbol>/<day>/snapshot")
def history_snapshot(symbol: str, day: str):
    try:
        index = int(request.args.get("index", "-1"))
        result = snapshot(symbol, day, index)
        if result is None:
            return jsonify({"error": "historical snapshot not found"}), 404
        return jsonify(result)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
