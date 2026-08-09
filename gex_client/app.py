from __future__ import annotations

import os
import time

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from gex_client.schwab import SchwabError, fetch_sanitized_snapshot, token_path


load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config.update(
    FOXCHASE_GEX_API_URL=os.getenv(
        "FOXCHASE_GEX_API_URL", "https://gex.foxchasetrading.com/api/community"
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
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def dashboard():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "foxchase-gex-local-client"})


@app.get("/api/setup-status")
def setup_status():
    """Report only whether local OAuth inputs exist; never return their values."""
    has_client_id = bool(os.getenv("SCHWAB_CLIENT_ID", "").strip())
    has_client_secret = bool(os.getenv("SCHWAB_CLIENT_SECRET", "").strip())
    has_token = token_path().is_file()
    return jsonify(
        {
            "ready": has_client_id and has_client_secret and has_token,
            "client_id_configured": has_client_id,
            "client_secret_configured": has_client_secret,
            "token_configured": has_token,
        }
    )


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
