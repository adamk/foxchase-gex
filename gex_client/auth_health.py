"""Sanitized Schwab authorization-age state and debounced operations alerts."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


# Schwab Trader API OAuth documentation: access tokens are valid for 30 minutes;
# refresh tokens have a non-sliding seven-day lifetime from interactive grant.
ACCESS_TOKEN_LIFETIME_SECONDS = 30 * 60
REAUTHORIZATION_LIFETIME_SECONDS = 7 * 24 * 60 * 60
WARNING_WINDOWS_SECONDS = (72 * 60 * 60, 24 * 60 * 60, 0)


def status_path() -> Path:
    configured = os.getenv("SCHWAB_AUTH_HEALTH_PATH")
    if configured:
        return Path(configured).expanduser()
    token = os.getenv("SCHWAB_TOKEN_PATH")
    if token:
        return Path(token).expanduser().with_name("schwab_auth_health.json")
    config = Path(os.getenv("FOXCHASE_GEX_CONFIG_DIR", "~/.foxchase-gex")).expanduser()
    return config / "schwab_auth_health.json"


def load_status() -> dict:
    try:
        value = json.loads(status_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(value: dict) -> None:
    destination = status_path()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".auth-health-", text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def record_interactive_authorization(now: float | None = None) -> dict:
    epoch = float(now if now is not None else time.time())
    previous = load_status()
    state = {
        "schema_version": 1,
        "interactive_authorized_at": _iso(epoch),
        "last_refresh_at": _iso(epoch),
        "reauthorization_due_at": _iso(epoch + REAUTHORIZATION_LIFETIME_SECONDS),
        "lifecycle_source": "schwab_trader_api_documentation",
        "health": "healthy",
        "last_failure_class": None,
        "alerts_sent": {},
        "updated_at": _iso(epoch),
    }
    if previous.get("health") in {"reauthorization_required", "authentication_failed"}:
        state["recovery_pending"] = True
    _write(state)
    return state


def record_refresh_success(now: float | None = None) -> dict:
    epoch = float(now if now is not None else time.time())
    state = load_status()
    state["schema_version"] = 1
    state["last_refresh_at"] = _iso(epoch)
    state["last_failure_class"] = None
    state["updated_at"] = _iso(epoch)
    # Deliberately do not move interactive_authorized_at or the hard deadline.
    if state.get("interactive_authorized_at"):
        state["health"] = authorization_state(state, epoch)["health"]
    _write(state)
    return state


def record_auth_failure(error_class: str, now: float | None = None) -> dict:
    epoch = float(now if now is not None else time.time())
    state = load_status()
    state["schema_version"] = 1
    state["health"] = "reauthorization_required"
    state["last_failure_class"] = str(error_class)[:120]
    state["failure_detected_at"] = state.get("failure_detected_at") or _iso(epoch)
    state["updated_at"] = _iso(epoch)
    _write(state)
    return state


def authorization_state(state: dict | None = None, now: float | None = None) -> dict:
    state = dict(state if state is not None else load_status())
    epoch = float(now if now is not None else time.time())
    authorized = state.get("interactive_authorized_at")
    if not authorized:
        return {"health": "unknown", "due_in_seconds": None, "due_at": None}
    auth_epoch = datetime.fromisoformat(str(authorized).replace("Z", "+00:00")).timestamp()
    due_epoch = auth_epoch + REAUTHORIZATION_LIFETIME_SECONDS
    remaining = due_epoch - epoch
    if state.get("health") == "reauthorization_required" or remaining <= 0:
        health = "reauthorization_required"
    elif remaining <= 72 * 60 * 60:
        health = "reauthorization_approaching"
    else:
        health = "healthy"
    return {"health": health, "due_in_seconds": remaining, "due_at": _iso(due_epoch)}


def due_alert_kind(state: dict | None = None, now: float | None = None) -> str | None:
    state = state if state is not None else load_status()
    view = authorization_state(state, now)
    remaining = view["due_in_seconds"]
    if remaining is None:
        return "metadata_missing"
    sent = state.get("alerts_sent") or {}
    if remaining <= 0 and not sent.get("urgent"):
        return "urgent"
    if remaining <= 24 * 60 * 60 and not sent.get("24h"):
        return "24h"
    if remaining <= 72 * 60 * 60 and not sent.get("72h"):
        return "72h"
    return None


def _message(kind: str, state: dict) -> str:
    view = authorization_state(state)
    if kind == "72h":
        return "Schwab reauthorization approaching: GEX authorization is due in approximately 3 days."
    if kind == "24h":
        return "Schwab reauthorization approaching: GEX authorization is due within approximately 24 hours."
    if kind in {"urgent", "required"}:
        return "Schwab reauthorization required: GEX collection will not function until interactive authorization is completed."
    if kind == "recovered":
        return "Schwab authorization recovered: token refresh and GEX authentication are healthy."
    return f"Schwab authorization age metadata is unavailable; due time cannot be determined ({view['health']})."


def send_dashboard_alert(kind: str, state: dict | None = None, now: float | None = None) -> bool:
    state = dict(state if state is not None else load_status())
    url = os.getenv("FOXCHASE_DASHBOARD_EVENT_URL", "").strip()
    token = os.getenv("FOXCHASE_DASHBOARD_EVENT_TOKEN", "").strip()
    if not url or not token:
        return False
    stamp = datetime.fromtimestamp(float(now if now is not None else time.time()), timezone.utc)
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "event_type": "system_alert",
                "setup": "GEX_SCHWAB_AUTH",
                "environment": "production",
                "node": "primary-pi",
                "timestamp": stamp.isoformat(),
                "event_id": f"gex-schwab-auth-{kind}-{stamp.date().isoformat()}",
                "event_key": f"gex-schwab-auth-{kind}-{stamp.date().isoformat()}",
                "notes": _message(kind, state),
            },
            timeout=10,
        )
        return response.ok
    except requests.RequestException:
        return False


def mark_alert_sent(kind: str, now: float | None = None) -> None:
    epoch = float(now if now is not None else time.time())
    state = load_status()
    state.setdefault("alerts_sent", {})[kind] = _iso(epoch)
    state["updated_at"] = _iso(epoch)
    _write(state)


def emit_due_alert(now: float | None = None) -> str | None:
    state = load_status()
    kind = due_alert_kind(state, now)
    if not kind:
        return None
    if send_dashboard_alert(kind, state, now):
        mark_alert_sent(kind, now)
        return kind
    return None


def emit_recovery_if_pending(now: float | None = None) -> bool:
    state = load_status()
    if not state.pop("recovery_pending", False):
        return False
    delivered = send_dashboard_alert("recovered", state, now)
    if not delivered:
        state["recovery_pending"] = True
    _write(state)
    return delivered
