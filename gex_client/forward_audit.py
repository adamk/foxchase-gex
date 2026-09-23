"""Shadow-only structural GEX audit derived from archived computed snapshots."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from gex_client.archive import archive_root


NY = ZoneInfo("America/New_York")
SCHEMA_VERSION = 1
METHOD_VERSION = "foxchase_shadow_gex_v1"
_lock = threading.Lock()


def _number(value) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _audit_path(symbol: str, day: str) -> Path:
    return archive_root() / "audit" / symbol.upper() / f"{day}.jsonl"


def _algo_context(day: str) -> Optional[dict]:
    """Read only non-broker metadata from the optional live algo state."""
    configured = os.getenv("FOXCHASE_ALGO_STATE_PATH", "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or str(state.get("trade_date")) != day:
        return None
    open_spread = state.get("open_spread") if isinstance(state.get("open_spread"), dict) else {}
    last_entry = state.get("last_entry_attempt") if isinstance(state.get("last_entry_attempt"), dict) else {}
    setup = open_spread.get("setup") or last_entry.get("setup")
    return {
        "trade_date": day,
        "entered_today": bool(state.get("entered_today")),
        "active_or_attempted_setup": setup,
        "last_trigger_time": state.get("last_trigger_time"),
    }


def _last_audit(symbol: str, day: str) -> Optional[dict]:
    path = _audit_path(symbol, day)
    if not path.is_file():
        return None
    last = None
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return last


def _movement(current: Optional[float], previous: Optional[float]) -> str:
    if current is None or previous is None:
        return "unknown"
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "unchanged"


def _strength_rank(label: object) -> int:
    return {"fragile": 0, "moderate": 1, "strong": 2, "dominant": 3}.get(
        str(label or "").lower(), -1
    )


def _decision_features(result: dict, previous: Optional[dict]) -> dict:
    structure = result.get("structure") if isinstance(result.get("structure"), dict) else {}
    prior = previous.get("features", {}) if isinstance(previous, dict) else {}
    spot = _number(result.get("spot"))
    flip = _number(structure.get("gamma_flip"))
    call_wall = _number(structure.get("call_wall"))
    put_wall = _number(structure.get("put_wall"))
    net_gex = _number(structure.get("net_gex"))
    flip_pct = _number(structure.get("flip_distance_pct"))
    return {
        "spot": spot,
        "net_gex": net_gex,
        "gamma_regime": structure.get("gamma_regime"),
        "gamma_flip": flip,
        "spot_minus_flip": _number(structure.get("spot_minus_flip")),
        "flip_distance_pct": flip_pct,
        "near_flip_0_15pct": flip_pct is not None and flip_pct <= 0.15,
        "call_wall": call_wall,
        "call_wall_strength": structure.get("call_wall_strength"),
        "call_wall_share": _number(structure.get("call_wall_share")),
        "call_wall_migration": _movement(call_wall, _number(prior.get("call_wall"))),
        "put_wall": put_wall,
        "put_wall_strength": structure.get("put_wall_strength"),
        "put_wall_share": _number(structure.get("put_wall_share")),
        "put_wall_migration": _movement(put_wall, _number(prior.get("put_wall"))),
        "gex_imbalance": _number(structure.get("gex_imbalance")),
        "method_version": METHOD_VERSION,
    }


def _shadow_decisions(features: dict) -> dict:
    spot, flip = features["spot"], features["gamma_flip"]
    call_wall, put_wall = features["call_wall"], features["put_wall"]
    positive = features["gamma_regime"] == "Positive"
    required = all(value is not None for value in (spot, flip, call_wall, put_wall))
    if not required:
        return {name: {"action": "INSUFFICIENT", "reasons": ["missing structural field"]}
                for name in ("bull_put", "call_credit", "iron_condor")}

    near = bool(features["near_flip_0_15pct"])
    put_rank = _strength_rank(features["put_wall_strength"])
    call_rank = _strength_rank(features["call_wall_strength"])

    bull_reasons = []
    if not positive:
        bull_reasons.append("negative gamma")
    if spot <= flip:
        bull_reasons.append("spot at/below gamma flip")
    if put_wall >= spot:
        bull_reasons.append("put wall not below spot")
    if features["put_wall_migration"] == "down":
        bull_reasons.append("put wall migrated down")
    bull_action = "BLOCK" if bull_reasons else (
        "HALF_SIZE" if near or put_rank < 1 else "ALLOW"
    )
    if not bull_reasons and near:
        bull_reasons.append("near gamma flip")
    if not bull_reasons and put_rank < 1:
        bull_reasons.append("put wall fragile")

    call_reasons = []
    if positive and spot >= flip:
        call_reasons.append("positive gamma with spot above flip")
    if call_wall <= spot:
        call_reasons.append("call wall not above spot")
    if features["call_wall_migration"] == "up":
        call_reasons.append("call wall migrated up")
    call_action = "BLOCK" if call_reasons else (
        "HALF_SIZE" if near or call_rank < 1 else "ALLOW"
    )
    if not call_reasons and near:
        call_reasons.append("near gamma flip")
    if not call_reasons and call_rank < 1:
        call_reasons.append("call wall fragile")

    condor_reasons = []
    if not positive:
        condor_reasons.append("negative gamma")
    if near:
        condor_reasons.append("near gamma flip")
    if not (put_wall < spot < call_wall):
        condor_reasons.append("spot not bracketed by walls")
    if put_rank < 1 or call_rank < 1:
        condor_reasons.append("one or both walls fragile")
    if features["put_wall_migration"] == "down" or features["call_wall_migration"] == "up":
        condor_reasons.append("walls expanding away from containment")
    condor_action = "ALLOW" if not condor_reasons else "BLOCK"

    return {
        "bull_put": {"action": bull_action, "reasons": bull_reasons or ["structure supportive"]},
        "call_credit": {"action": call_action, "reasons": call_reasons or ["structure supportive"]},
        "iron_condor": {"action": condor_action, "reasons": condor_reasons or ["positive gamma and walls bracket spot"]},
    }


def archive_forward_audit(symbol: str, result: dict, captured_at: Optional[datetime] = None) -> Path:
    """Append replayable features and provisional shadow decisions.

    Decisions never touch the broker or the live strategy. Raw features are
    retained so every threshold can be re-scored later without recollection.
    """
    display_symbol = symbol.upper().strip()
    if display_symbol not in {"SPX", "NDX"}:
        raise ValueError("supported symbols are SPX and NDX")
    stamp = (captured_at or datetime.now(NY)).astimezone(NY)
    day = stamp.date().isoformat()
    with _lock:
        previous = _last_audit(display_symbol, day)
        features = _decision_features(result, previous)
        record = {
            "schema_version": SCHEMA_VERSION,
            "captured_at": stamp.isoformat(timespec="seconds"),
            "symbol": display_symbol,
            "source_updated_at": result.get("updated_at"),
            "algo_context": _algo_context(day),
            "features": features,
            "shadow_decisions": _shadow_decisions(features),
        }
        destination = _audit_path(display_symbol, day)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return destination
