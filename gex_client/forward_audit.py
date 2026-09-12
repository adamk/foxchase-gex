"""Shadow-only structural GEX audit derived from archived computed snapshots."""

from __future__ import annotations

import json
import math
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from gex_client.archive import archive_root


NY = ZoneInfo("America/New_York")
SCHEMA_VERSION = 1
METHOD_VERSION = "foxchase_shadow_gex_v2"
_lock = threading.Lock()


def _publish_r1_summary(summary: dict) -> None:
    """Best-effort dashboard telemetry; collection must never depend on it."""
    url = os.getenv("FOXCHASE_GEX_FORWARD_DASHBOARD_URL", "").strip()
    if not url:
        event_url = os.getenv("FOXCHASE_DASHBOARD_EVENT_URL", "").strip()
        if event_url.endswith("/api/bot-events"):
            url = event_url[:-len("/api/bot-events")] + "/api/gex-forward"
    token = os.getenv("FOXCHASE_DASHBOARD_EVENT_TOKEN", "").strip()
    if not url or not token:
        return
    request = urllib.request.Request(
        url,
        data=json.dumps(summary, separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read(256)
    except (OSError, urllib.error.URLError):
        pass


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
    forward = state.get("gex_forward_context") if isinstance(state.get("gex_forward_context"), dict) else {}
    return {
        "trade_date": day,
        "entered_today": bool(state.get("entered_today")),
        "active_or_attempted_setup": setup,
        "last_trigger_time": state.get("last_trigger_time"),
        "gex_forward_context": forward,
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


def _study_window(stamp: datetime) -> Optional[str]:
    minute = stamp.hour * 60 + stamp.minute
    for label, start, end in (
        ("09:45-10:15", 9 * 60 + 45, 10 * 60 + 15),
        ("10:15-11:00", 10 * 60 + 15, 11 * 60),
        ("11:00-12:30", 11 * 60, 12 * 60 + 30),
        ("12:30-14:30", 12 * 60 + 30, 14 * 60 + 30),
    ):
        if start <= minute < end:
            return label
    return None


def _r1_spx_em_confluence(
    features: dict, algo_context: Optional[dict], stamp: datetime, direction: str
) -> dict:
    """Score a shadow-only R1 SPX wall/EM observation in either direction."""
    context = (algo_context or {}).get("gex_forward_context")
    context = context if isinstance(context, dict) else {}
    bearish = direction == "bear_call"
    wall_name = "call_wall" if bearish else "put_wall"
    raw_em_name = "raw_spx_em_upper" if bearish else "raw_spx_em_lower"
    mapped_em_name = "spx_mapped_em_upper" if bearish else "spx_mapped_em_lower"
    result = {
        "action": "WATCH",
        "reasons": [],
        "direction": direction,
        "entry_window": _study_window(stamp),
        "spread_width": 3.0,
        "min_credit": 0.18,
        "profit_target_pct": 0.60,
        "stop_multiplier": 2.0,
        "exit_time": "15:00 ET",
        "method_version": METHOD_VERSION,
    }
    required = {
        wall_name: features.get(wall_name),
        "spot": features.get("spot"),
        "gamma_flip": features.get("gamma_flip"),
        "spy_open": _number(context.get("spy_open")),
        mapped_em_name: _number(context.get(mapped_em_name)),
        raw_em_name: _number(context.get(raw_em_name)),
        "raw_spx_parity_center": _number(context.get("raw_spx_parity_center")),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        result["reasons"] = ["missing " + ", ".join(missing)]
        return result

    wall = float(required[wall_name])
    spot = float(required["spot"])
    flip = float(required["gamma_flip"])
    mapped_em = float(required[mapped_em_name])
    scale = float(required["spy_open"]) / float(required["raw_spx_parity_center"])
    mapped_wall = mapped_em + (wall - float(required[raw_em_name])) * scale
    boundary = max(mapped_wall, mapped_em) if bearish else min(mapped_wall, mapped_em)
    short_strike = math.ceil(boundary) if bearish else math.floor(boundary)
    result.update({
        "spx_reference_wall": wall,
        "spx_spot": spot,
        "spx_gamma_flip": flip,
        "spy_mapped_wall": round(mapped_wall, 4),
        "spy_mapped_em": round(mapped_em, 4),
        "confluence_distance_spy": round(abs(mapped_wall - mapped_em), 4),
        "proposed_short_strike": float(short_strike),
        "proposed_long_strike": float(short_strike + 3 if bearish else short_strike - 3),
    })

    blockers = []
    if str(context.get("regime")) != "R1":
        blockers.append("algo regime is not R1")
    if result["entry_window"] is None:
        blockers.append("outside discovery windows")
    if bearish:
        if features.get("gamma_regime") != "Negative": blockers.append("SPX gamma is not negative")
        if spot >= flip: blockers.append("SPX spot is not below gamma flip")
        if wall <= spot: blockers.append("call wall is not above SPX spot")
        if _strength_rank(features.get("call_wall_strength")) < 1: blockers.append("call wall is weaker than Moderate")
        if features.get("call_wall_migration") == "down": blockers.append("call wall migrated down")
    else:
        if features.get("gamma_regime") != "Positive": blockers.append("SPX gamma is not positive")
        if spot <= flip: blockers.append("SPX spot is not above gamma flip")
        if wall >= spot: blockers.append("put wall is not below SPX spot")
        if _strength_rank(features.get("put_wall_strength")) < 1: blockers.append("put wall is weaker than Moderate")
        if features.get("put_wall_migration") == "up": blockers.append("put wall migrated up")
    if abs(mapped_wall - mapped_em) > 0.50:
        blockers.append("mapped wall is more than $0.50 from mapped expected-move boundary")
    result["action"] = "CANDIDATE" if not blockers else "WATCH"
    result["reasons"] = blockers or [f"R1 SPX {direction} wall/EM confluence qualified"]
    return result


def _write_r1_summary(day: str) -> None:
    """Update the first candidate per direction and discovery window."""
    source = _audit_path("SPX", day)
    try:
        rows = [json.loads(line) for line in source.open("r", encoding="utf-8") if line.strip()]
    except (OSError, json.JSONDecodeError):
        return
    selected = {}
    for index, row in enumerate(rows):
        for key in ("r1_spx_bear_call_confluence", "r1_spx_bull_put_confluence"):
            plan = row.get("shadow_decisions", {}).get(key, {})
            if plan.get("action") == "CANDIDATE":
                identity = (plan.get("direction"), plan.get("entry_window"))
                selected.setdefault(identity, (index, row, plan))
    if not selected:
        return
    results = []
    last_stamp = rows[-1].get("captured_at")
    for (_, _), (index, signal, plan) in selected.items():
        spots = [_number(row.get("features", {}).get("spot")) for row in rows[index:]]
        spots = [value for value in spots if value is not None]
        if not spots: continue
        complete = bool(last_stamp and last_stamp[11:16] >= "15:55")
        wall = _number(plan.get("spx_reference_wall"))
        bearish = plan.get("direction") == "bear_call"
        breached = wall is not None and (max(spots) >= wall if bearish else min(spots) <= wall)
        results.append({
            "direction": plan.get("direction"), "entry_window": plan.get("entry_window"),
            "status": "COMPLETE" if complete else "OPEN", "signal_time": signal.get("captured_at"),
            "proposed_short_strike": plan.get("proposed_short_strike"),
            "proposed_long_strike": plan.get("proposed_long_strike"),
            "reference_spx_wall": wall,
            "max_spx_after_signal": max(spots), "min_spx_after_signal": min(spots),
            "structural_outcome": "BREACHED_WALL" if breached else "HELD_WALL" if complete else "PENDING",
        })
    summary = {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "trade_date": day,
        "status": "COMPLETE" if results and all(item["status"] == "COMPLETE" for item in results) else "OPEN",
        "candidates": results,
        "note": "Structural shadow result only; executable option credit and spread mark-to-market are not inferred.",
        "updated_at": last_stamp,
    }
    destination = archive_root() / "audit" / "strategy" / "r1_spx_call_confluence" / f"{day}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    _publish_r1_summary(summary)


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
        algo_context = _algo_context(day)
        decisions = _shadow_decisions(features)
        if display_symbol == "SPX":
            decisions["r1_spx_bear_call_confluence"] = _r1_spx_em_confluence(features, algo_context, stamp, "bear_call")
            decisions["r1_spx_bull_put_confluence"] = _r1_spx_em_confluence(features, algo_context, stamp, "bull_put")
        record = {
            "schema_version": SCHEMA_VERSION,
            "captured_at": stamp.isoformat(timespec="seconds"),
            "symbol": display_symbol,
            "source_updated_at": result.get("updated_at"),
            "algo_context": algo_context,
            "features": features,
            "shadow_decisions": decisions,
        }
        destination = _audit_path(display_symbol, day)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if display_symbol == "SPX":
            _write_r1_summary(day)
    return destination
