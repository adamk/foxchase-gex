"""Persistent, debounced collector health state; contains no credentials."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gex_client.archive import archive_root

NY = ZoneInfo("America/New_York")
FAILURE_THRESHOLD = max(2, int(os.getenv("FOXCHASE_GEX_ALERT_FAILURES", "5")))


def _path(day: str) -> Path:
    return archive_root() / "status" / f"{day}.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"symbols": {}, "alerts": []}


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".gex-health-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def expected_frames(now: datetime, cadence_seconds: int) -> int:
    local = now.astimezone(NY)
    start = local.replace(hour=9, minute=30, second=0, microsecond=0)
    end = local.replace(hour=16, minute=0, second=0, microsecond=0)
    if local < start:
        return 0
    elapsed = min(local, end) - start
    return int(elapsed.total_seconds() // cadence_seconds) + 1


def record_attempt(symbol: str, success: bool, now: datetime, cadence_seconds: int, error: str = "") -> dict:
    local = now.astimezone(NY)
    path = _path(local.date().isoformat())
    state = _load(path)
    row = state.setdefault("symbols", {}).setdefault(symbol, {
        "attempts": 0, "successful_frames": 0, "consecutive_failures": 0,
        "first_success": None, "last_success": None, "health": "initializing",
    })
    row["attempts"] += 1
    row["expected_frames"] = expected_frames(local, cadence_seconds)
    prior_health = row["health"]
    if success:
        row["successful_frames"] += 1
        row["consecutive_failures"] = 0
        row["first_success"] = row["first_success"] or local.isoformat(timespec="seconds")
        row["last_success"] = local.isoformat(timespec="seconds")
        row["health"] = "healthy"
        row["last_error_class"] = None
    else:
        row["consecutive_failures"] += 1
        row["last_error_class"] = error[:160]
        if row["consecutive_failures"] >= FAILURE_THRESHOLD:
            row["health"] = "degraded"
    row["missing_frames"] = max(0, row["expected_frames"] - row["successful_frames"])
    alert = None
    if row["health"] != prior_health and row["health"] in {"degraded", "healthy"}:
        alert = {
            "timestamp": local.isoformat(timespec="seconds"), "symbol": symbol,
            "transition": f"{prior_health}->{row['health']}",
        }
        state.setdefault("alerts", []).append(alert)
    state["updated_at"] = local.isoformat(timespec="seconds")
    _atomic_write(path, state)
    return {"state": state, "alert": alert}
