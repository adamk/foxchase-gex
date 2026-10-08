"""Nonsecret full-OAuth lifecycle metadata for operator-facing status."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from gex_client.auth_health import REAUTHORIZATION_LIFETIME_SECONDS, load_status


FIELDS = ("last_full_oauth_at", "next_reauth_due_at")


def metadata_path() -> Path:
    configured = os.getenv("SCHWAB_AUTH_LIFECYCLE_PATH")
    if configured:
        return Path(configured).expanduser()
    token = os.getenv("SCHWAB_TOKEN_PATH")
    if token:
        return Path(token).expanduser().with_name("schwab_auth_lifecycle.json")
    config = Path(os.getenv("FOXCHASE_GEX_CONFIG_DIR", "~/.foxchase-gex")).expanduser()
    return config / "schwab_auth_lifecycle.json"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _epoch(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _atomic_write(value: dict) -> None:
    destination = metadata_path()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=".schwab-lifecycle-", text=True
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record_full_oauth_authorization(now: float | None = None) -> dict:
    """Persist lifecycle age only after a successful full auth-code exchange."""
    epoch = float(now if now is not None else time.time())
    value = {
        "last_full_oauth_at": _iso(epoch),
        "next_reauth_due_at": _iso(epoch + REAUTHORIZATION_LIFETIME_SECONDS),
    }
    _atomic_write(value)
    return value


def load_lifecycle(migrate_legacy: bool = False) -> dict | None:
    """Read the exact two-field record, optionally migrating trusted old metadata."""
    path = metadata_path()
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict) or set(value) != set(FIELDS):
            return None
        if any(_epoch(value.get(field)) is None for field in FIELDS):
            return None
        return {field: value[field] for field in FIELDS}

    if not migrate_legacy:
        return None

    # Older releases kept these exact full-interactive-auth timestamps in the
    # nonsecret auth-health record. Never use access-token refresh timestamps.
    old = load_status()
    last = old.get("interactive_authorized_at")
    due = old.get("reauthorization_due_at")
    last_epoch = _epoch(last)
    due_epoch = _epoch(due)
    if last_epoch is None:
        return None
    if due_epoch is None:
        due_epoch = last_epoch + REAUTHORIZATION_LIFETIME_SECONDS
    if abs(due_epoch - (last_epoch + REAUTHORIZATION_LIFETIME_SECONDS)) > 1:
        return None
    value = {
        "last_full_oauth_at": _iso(last_epoch),
        "next_reauth_due_at": _iso(due_epoch),
    }
    try:
        _atomic_write(value)
    except OSError:
        # The known legacy record remains usable for this response. A later
        # successful full authorization will write the dedicated file.
        return value
    return value


def operator_status(now: float | None = None) -> dict:
    epoch = float(now if now is not None else time.time())
    lifecycle = load_lifecycle(migrate_legacy=True)
    latch = load_status()
    latched_failure = latch.get("health") == "reauthorization_required"

    if lifecycle is None:
        return {
            "state": "reauth_required",
            "last_full_oauth_at": None,
            "next_reauth_due_at": None,
            "remaining_seconds": None,
            "reauth_required": True,
        }

    last_epoch = _epoch(lifecycle["last_full_oauth_at"])
    due_epoch = _epoch(lifecycle["next_reauth_due_at"])
    if (
        last_epoch is None
        or due_epoch is None
        or last_epoch > epoch
        or abs(due_epoch - (last_epoch + REAUTHORIZATION_LIFETIME_SECONDS)) > 1
    ):
        return {
            "state": "reauth_required",
            "last_full_oauth_at": None,
            "next_reauth_due_at": None,
            "remaining_seconds": None,
            "reauth_required": True,
        }

    age = epoch - last_epoch
    remaining = max(0, int(due_epoch - epoch))
    if latched_failure or age >= 7 * 86400 or due_epoch <= epoch:
        state = "reauth_required"
    elif age >= 6 * 86400:
        state = "due_soon"
    else:
        state = "healthy"
    return {
        "state": state,
        "last_full_oauth_at": lifecycle["last_full_oauth_at"],
        "next_reauth_due_at": lifecycle["next_reauth_due_at"],
        "remaining_seconds": remaining,
        "reauth_required": state == "reauth_required",
    }
