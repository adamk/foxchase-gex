"""Headless market-hours collector for durable GEX history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from gex_client.archive import archive_snapshot, verify_archive_mount
from gex_client.auth_health import (
    authorization_state, emit_due_alert, emit_recovery_if_pending, load_status,
    mark_alert_sent, send_dashboard_alert,
)
from gex_client.forward_audit import archive_forward_audit
from gex_client.health import record_attempt
from gex_client.schwab import fetch_sanitized_snapshot, get_access_token


NY = ZoneInfo("America/New_York")


def in_collection_window(now: datetime) -> bool:
    local = now.astimezone(NY)
    minute = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 + 30 <= minute <= 16 * 60


def _source_hash(path: str) -> str:
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def collect_once(base_url: str, symbol: str, session_id: str) -> None:
    verify_archive_mount()
    request_stamp = datetime.now(NY).isoformat(timespec="microseconds")
    causal_input = fetch_sanitized_snapshot(symbol)
    compute_url = os.getenv(
        "FOXCHASE_GEX_API_URL", "https://compute.foxchasetrading.com/api/community"
    ).rstrip("/")
    response = requests.post(
        f"{compute_url}/gex",
        headers={"X-GEX-Session": session_id, "Accept": "application/json"},
        json=causal_input,
        timeout=55,
    )
    response_stamp = datetime.now(NY).isoformat(timespec="microseconds")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("local GEX service returned invalid JSON") from exc
    if not response.ok:
        raise RuntimeError(str(result.get("error", f"HTTP {response.status_code}")))
    # Presence is transient and is not meaningful in a historical record.
    result.pop("online", None)
    result.pop("client_cached", None)
    result.pop("client_cache_age_seconds", None)
    method = (result.get("structure") or {}).get("method_version")
    method_config = {
        "method_version": method,
        "source": result.get("source"),
        "unit": result.get("unit"),
        "symbol": symbol,
        "expiration_scope": "0DTE",
    }
    archive_snapshot(
        symbol, result, causal_input=causal_input,
        request_timestamp=request_stamp, response_timestamp=response_stamp,
        provenance={
            "provider": "Schwab",
            "collector_code_hash": _source_hash(__file__),
            "methodology_config_hash": hashlib.sha256(
                json.dumps(method_config, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
    )
    if os.getenv("FOXCHASE_GEX_FORWARD_AUDIT", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }:
        archive_forward_audit(symbol, result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive computed SPX and NDX GEX snapshots")
    parser.add_argument(
        "--base-url",
        default=os.getenv("FOXCHASE_GEX_LOCAL_URL", "http://127.0.0.1:8765"),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("FOXCHASE_GEX_CAPTURE_SECONDS", "60")),
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    interval = max(30, args.interval)
    session_id = "foxchase-gex-headless-collector"
    auth_probe_day = None
    terminal_auth_failure = False
    last_terminal_reprobe = 0.0

    while True:
        now = datetime.now(NY)
        minute = now.hour * 60 + now.minute
        if now.weekday() < 5 and 8 * 60 + 45 <= minute < 9 * 60 + 30 and auth_probe_day != now.date():
            lifecycle = authorization_state(load_status())
            if lifecycle["health"] == "reauthorization_required":
                terminal_auth_failure = True
                state = load_status()
                if not (state.get("alerts_sent") or {}).get("required"):
                    if send_dashboard_alert("required", state):
                        mark_alert_sent("required")
                print(f"{now.isoformat(timespec='seconds')} [GEXAlert] reauthorization required before market open", flush=True)
            else:
                try:
                    get_access_token()
                    terminal_auth_failure = False
                    emit_due_alert()
                    emit_recovery_if_pending()
                    print(f"{now.isoformat(timespec='seconds')} auth probe PASS", flush=True)
                except Exception as exc:
                    state = load_status()
                    terminal_auth_failure = state.get("health") == "reauthorization_required"
                    if terminal_auth_failure and not (state.get("alerts_sent") or {}).get("required"):
                        if send_dashboard_alert("required", state):
                            mark_alert_sent("required")
                    print(f"{now.isoformat(timespec='seconds')} [GEXAlert] auth probe FAILED: {type(exc).__name__}", flush=True)
            auth_probe_day = now.date()
        if args.once or in_collection_window(now):
            # A terminal OAuth failure is one incident, not 385 provider calls.
            # Probe no more than every 15 minutes so a completed interactive
            # reauthorization can recover without restarting this collector.
            if terminal_auth_failure:
                if time.time() - last_terminal_reprobe < 900:
                    if args.once:
                        break
                    time.sleep(interval)
                    continue
                last_terminal_reprobe = time.time()
                try:
                    get_access_token()
                    terminal_auth_failure = False
                    emit_recovery_if_pending()
                except Exception:
                    if args.once:
                        break
                    time.sleep(interval)
                    continue
            for symbol in ("SPX", "NDX"):
                try:
                    # The calculation service rate-limits each anonymous session.
                    # Give the independently collected symbols stable, separate
                    # identities so a successful SPX request cannot throttle NDX.
                    collect_once(args.base_url, symbol, f"{session_id}-{symbol.lower()}")
                    outcome = record_attempt(symbol, True, datetime.now(NY), interval)
                    if outcome["alert"]:
                        print(f"{datetime.now(NY).isoformat(timespec='seconds')} [GEXAlert] {symbol} collector recovered", flush=True)
                    print(f"{datetime.now(NY).isoformat(timespec='seconds')} archived {symbol}", flush=True)
                except Exception as exc:
                    outcome = record_attempt(symbol, False, datetime.now(NY), interval, type(exc).__name__)
                    if outcome["alert"]:
                        print(f"{datetime.now(NY).isoformat(timespec='seconds')} [GEXAlert] {symbol} collector degraded after repeated failures", flush=True)
                    print(
                        f"{datetime.now(NY).isoformat(timespec='seconds')} {symbol} failed: {exc}",
                        flush=True,
                    )
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
