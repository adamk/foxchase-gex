"""Local-only Schwab OAuth, chain retrieval, and payload minimization."""

from __future__ import annotations

import base64
import fcntl
import hmac
import json
import logging
import math
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from gex_client.auth_health import (
    authorization_state,
    load_status,
    record_auth_failure,
    record_pending_interactive_authorization,
    record_refresh_success,
)
from gex_client.auth_lifecycle import record_full_oauth_authorization
from gex_client.local_setup import CredentialStoreError, configured_credentials


load_dotenv()

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
MARKET_DATA_BASE = "https://api.schwabapi.com/marketdata/v1"
NY = ZoneInfo("America/New_York")
_REFRESH_LOGGER = logging.getLogger("foxchase.gex.schwab")
_REFRESH_LOGGER.setLevel(logging.INFO)
if not _REFRESH_LOGGER.handlers:
    _REFRESH_LOGGER.addHandler(logging.StreamHandler())
_REFRESH_LOGGER.propagate = False


class SchwabError(RuntimeError):
    pass


def _config_dir() -> Path:
    configured = os.getenv("FOXCHASE_GEX_CONFIG_DIR", "~/.foxchase-gex")
    return Path(configured).expanduser()


def token_path() -> Path:
    configured = os.getenv("SCHWAB_TOKEN_PATH")
    return Path(configured).expanduser() if configured else _config_dir() / "schwab_tokens.json"


def _credentials() -> tuple[str, str, str]:
    try:
        return configured_credentials()
    except CredentialStoreError as exc:
        raise SchwabError(str(exc)) from exc


def authorization_url(state: str | None = None) -> str:
    client_id, _, redirect_uri = _credentials()
    parameters = {"client_id": client_id, "redirect_uri": redirect_uri}
    if state is not None:
        if not isinstance(state, str) or not 32 <= len(state) <= 1024:
            raise SchwabError("OAuth state is invalid")
        parameters["state"] = state
    return f"{AUTH_URL}?{urlencode(parameters)}"


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


@contextmanager
def _token_store_lock():
    """Serialize token reads/writes across dashboard and collector processes."""
    destination = token_path()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = destination.with_name(destination.name + ".lock")
    lock_path.touch(mode=0o600, exist_ok=True)
    os.chmod(lock_path, 0o600)
    with lock_path.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _save_tokens_unlocked(tokens: dict) -> None:
    destination = token_path()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = dict(tokens)
    payload["saved_at"] = int(time.time())
    handle, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=".schwab_tokens.", text=True
    )
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def save_tokens(tokens: dict) -> None:
    destination = token_path()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _token_store_lock():
        _save_tokens_unlocked(tokens)


def load_tokens() -> dict | None:
    source = token_path()
    if not source.exists():
        return None
    try:
        with source.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise SchwabError(f"could not read Schwab token file: {exc}") from exc
    return value if isinstance(value, dict) else None


def exchange_authorization_response(value: str, expected_state: str | None = None) -> dict:
    value = value.strip()
    if not value:
        raise SchwabError("authorization response is empty")
    state_values = []
    if "://" in value:
        query = parse_qs(urlparse(value).query)
        code = query.get("code", [""])[0]
        state_values = query.get("state", [])
    else:
        code = value
    if not code:
        raise SchwabError("the pasted value did not contain an authorization code")
    if expected_state is not None and (
        not isinstance(expected_state, str)
        or len(expected_state) < 32
        or len(state_values) != 1
        or not hmac.compare_digest(state_values[0], expected_state)
    ):
        raise SchwabError("the pasted authorization response did not match the expected OAuth state")

    client_id, client_secret, redirect_uri = _credentials()
    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if not response.ok:
        raise SchwabError(
            f"Schwab token exchange failed ({response.status_code}): {response.text[:500]}"
        )
    tokens = response.json()
    save_tokens(tokens)
    record_pending_interactive_authorization()
    try:
        record_full_oauth_authorization()
    except OSError as exc:
        raise SchwabError(
            "authorization succeeded, but the nonsecret lifecycle metadata could not be saved"
        ) from exc
    return tokens


def _access_token_is_fresh(tokens: dict, now: float | None = None) -> bool:
    try:
        epoch = time.time() if now is None else float(now)
        saved_at = int(tokens.get("saved_at", 0))
        expires_in = int(tokens.get("expires_in", 1800))
        return bool(tokens.get("access_token")) and epoch < saved_at + expires_in - 90
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def _oauth_error_name(payload) -> str:
    """Extract permanent OAuth failures even when an upstream wraps JSON."""
    permanent = ("invalid_grant", "invalid_client")
    if isinstance(payload, dict):
        for key in ("error", "error_description"):
            value = payload.get(key)
            if isinstance(value, str):
                for name in permanent:
                    if value == name or name in value:
                        return name
                try:
                    nested = json.loads(value)
                except (TypeError, ValueError):
                    nested = None
                nested_name = _oauth_error_name(nested)
                if nested_name in permanent:
                    return nested_name
        return str(payload.get("error") or "refresh_failed")
    return "refresh_failed"


def _reauthorization_required() -> bool:
    state = load_status()
    return (
        state.get("health") == "reauthorization_required"
        or authorization_state(state)["health"] == "reauthorization_required"
    )


def _log_refresh_event(
    event: str,
    *,
    reason: str | None = None,
    http_status: int | None = None,
    error_class: str | None = None,
) -> None:
    """Emit a token-free refresh lifecycle record for multi-process diagnosis."""
    fields = [
        f"event={event}",
        f"pid={os.getpid()}",
        f"timestamp={datetime.now(timezone.utc).isoformat(timespec='milliseconds')}",
    ]
    if reason in {"another_worker_refreshed", "already_fresh"}:
        fields.append(f"reason={reason}")
    if http_status is not None:
        fields.append(f"http_status={int(http_status)}")
    if error_class is not None:
        safe_error = (
            error_class
            if error_class
            in {
                "invalid_grant",
                "invalid_client",
                "provider_error",
                "Timeout",
                "ConnectionError",
                "RequestException",
                "InvalidJSON",
                "MalformedTokenResponse",
                "TokenWriteError",
                "MissingRefreshToken",
                "ReauthorizationRequired",
            }
            else "provider_error"
        )
        fields.append(f"error_class={safe_error}")
    _REFRESH_LOGGER.info("schwab_refresh %s", " ".join(fields))


def _refresh_tokens(tokens: dict) -> dict:
    with _token_store_lock():
        _log_refresh_event("refresh_lock_acquired")
        current = load_tokens()
        _log_refresh_event("post_lock_token_reloaded")
        if isinstance(current, dict) and _access_token_is_fresh(current):
            reason = (
                "another_worker_refreshed"
                if not _access_token_is_fresh(tokens)
                else "already_fresh"
            )
            _log_refresh_event("refresh_skipped_after_lock", reason=reason)
            return current
        if _reauthorization_required():
            _log_refresh_event(
                "refresh_failed", error_class="ReauthorizationRequired"
            )
            raise SchwabError("Schwab reauthorization required; run `python -m gex_client.login`")
        current = current if isinstance(current, dict) else tokens
        if not isinstance(current, dict):
            _log_refresh_event("refresh_failed", error_class="MalformedTokenResponse")
            raise SchwabError("Schwab token state is malformed; run the login command again")
        refresh_token = current.get("refresh_token")
        if not refresh_token:
            _log_refresh_event("refresh_failed", error_class="MissingRefreshToken")
            raise SchwabError("refresh token is missing; run the login command again")
        client_id, client_secret, _ = _credentials()
        _log_refresh_event("refresh_request_initiated")
        try:
            response = requests.post(
                TOKEN_URL,
                headers={
                    "Authorization": _basic_auth_header(client_id, client_secret),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                timeout=30,
            )
        except requests.Timeout:
            _log_refresh_event("refresh_failed", error_class="Timeout")
            raise
        except requests.ConnectionError:
            _log_refresh_event("refresh_failed", error_class="ConnectionError")
            raise
        except requests.RequestException:
            _log_refresh_event("refresh_failed", error_class="RequestException")
            raise
        if not response.ok:
            try:
                payload = response.json()
            except (TypeError, ValueError):
                payload = None
            error_name = _oauth_error_name(payload)
            safe_error = (
                error_name if error_name in {"invalid_grant", "invalid_client"} else "provider_error"
            )
            _log_refresh_event(
                "refresh_failed", http_status=response.status_code, error_class=safe_error
            )
            if error_name in {"invalid_grant", "invalid_client"}:
                record_auth_failure(error_name)
            raise SchwabError(
                f"Schwab token refresh failed ({response.status_code}): {response.text[:500]}"
            )
        try:
            refreshed = response.json()
        except (TypeError, ValueError) as exc:
            _log_refresh_event(
                "refresh_failed", http_status=response.status_code, error_class="InvalidJSON"
            )
            raise SchwabError("Schwab token refresh returned invalid JSON") from exc
        if not isinstance(refreshed, dict) or not refreshed.get("access_token"):
            _log_refresh_event(
                "refresh_failed", http_status=response.status_code, error_class="MalformedTokenResponse"
            )
            raise SchwabError("Schwab token refresh returned malformed token data")
        refreshed.setdefault("refresh_token", refresh_token)
        try:
            _save_tokens_unlocked(refreshed)
        except OSError:
            _log_refresh_event("refresh_failed", error_class="TokenWriteError")
            raise
        record_refresh_success()
        _log_refresh_event("refresh_succeeded", http_status=response.status_code)
        return refreshed


def get_access_token() -> str:
    tokens = load_tokens()
    if not tokens:
        raise SchwabError("no local Schwab token found; run `python -m gex_client.login`")
    saved_at = int(tokens.get("saved_at", 0))
    expires_in = int(tokens.get("expires_in", 1800))
    if time.time() >= saved_at + expires_in - 90:
        if _reauthorization_required():
            raise SchwabError("Schwab reauthorization required; run `python -m gex_client.login`")
        tokens = _refresh_tokens(tokens)
    access_token = tokens.get("access_token")
    if not access_token:
        raise SchwabError("local Schwab token file has no access token")
    return str(access_token)


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _bounded_float(value, minimum: float, maximum: float, default: float = 0.0) -> float:
    result = _safe_float(value, default)
    if not math.isfinite(result) or result < minimum or result > maximum:
        return float(default)
    return result


def _spot_from_chain(*chains: dict) -> float:
    for chain in chains:
        for key in ("underlyingPrice", "underlying_price", "lastPrice"):
            value = _safe_float(chain.get(key))
            if value > 0:
                return value
        underlying = chain.get("underlying")
        if isinstance(underlying, dict):
            for key in ("last", "mark", "close", "quote"):
                value = _safe_float(underlying.get(key))
                if value > 0:
                    return value
    raise SchwabError("Schwab response did not include the underlying price")


def _extract_minimum_contracts(chain_map: dict, option_type: str, expiration: str) -> list[dict]:
    rows: list[dict] = []
    if not isinstance(chain_map, dict):
        return rows
    for expiration_key, strikes in chain_map.items():
        if str(expiration_key).split(":", 1)[0] != expiration or not isinstance(strikes, dict):
            continue
        for strike_key, contracts in strikes.items():
            if not isinstance(contracts, list):
                continue
            for contract in contracts:
                if not isinstance(contract, dict):
                    continue
                strike = _safe_float(strike_key)
                if strike <= 0:
                    continue
                rows.append(
                    {
                        "option_type": option_type,
                        "strike": strike,
                        "open_interest": _bounded_float(
                            contract.get("openInterest", contract.get("open_interest", 0)),
                            0.0,
                            100_000_000.0,
                        ),
                        "gamma": _bounded_float(
                            contract.get("gamma", contract.get("theoreticalOptionValueGamma", 0)),
                            0.0,
                            10.0,
                        ),
                        "volatility": _bounded_float(
                            contract.get(
                                "volatility",
                                contract.get("impliedVolatility", contract.get("iv", 0)),
                            ),
                            0.0,
                            1_000.0,
                        ),
                        "multiplier": _bounded_float(
                            contract.get("multiplier", 100), 1.0, 1_000.0, 100.0
                        ),
                    }
                )
    return rows


def sanitize_chains(
    symbol: str,
    call_chain: dict,
    put_chain: dict,
    expiration: str | None = None,
) -> dict:
    """Return the complete and intentionally small EC2 request payload."""
    display_symbol = symbol.upper().strip().replace("$", "").replace(".", "")
    if display_symbol not in {"SPX", "NDX"}:
        raise SchwabError("supported symbols are SPX and NDX")
    expiration = expiration or datetime.now(NY).date().isoformat()
    contracts = _extract_minimum_contracts(
        call_chain.get("callExpDateMap", {}), "CALL", expiration
    ) + _extract_minimum_contracts(
        put_chain.get("putExpDateMap", {}), "PUT", expiration
    )
    if not contracts:
        raise SchwabError(f"Schwab returned no 0DTE contracts for {expiration}")
    return {
        "symbol": display_symbol,
        "spot": round(_spot_from_chain(call_chain, put_chain), 4),
        "expiration_date": expiration,
        "contracts": contracts,
    }


def fetch_sanitized_snapshot(symbol: str) -> dict:
    display_symbol = symbol.upper().strip().replace("$", "").replace(".", "")
    if display_symbol not in {"SPX", "NDX"}:
        raise SchwabError("supported symbols are SPX and NDX")
    schwab_symbol = f"${display_symbol}"
    strike_count = 40 if display_symbol == "SPX" else 100
    expiration = datetime.now(NY).date().isoformat()
    headers = {"Authorization": f"Bearer {get_access_token()}", "Accept": "application/json"}

    def request_side(contract_type: str, count: int) -> dict:
        response = requests.get(
            f"{MARKET_DATA_BASE}/chains",
            headers=headers,
            params={
                "symbol": schwab_symbol,
                "contractType": contract_type,
                "strategy": "SINGLE",
                "strikeCount": count,
                "fromDate": expiration,
                "toDate": expiration,
            },
            timeout=30,
        )
        if not response.ok:
            raise SchwabError(
                f"Schwab chain request failed for {contract_type} "
                f"({response.status_code}): {response.text[:500]}"
            )
        return response.json()

    counts = [strike_count] + [
        count for count in (80, 70, 60, 50, 40, 30) if count < strike_count
    ]
    last_error: SchwabError | None = None
    for count in counts:
        try:
            return sanitize_chains(
                display_symbol,
                request_side("CALL", count),
                request_side("PUT", count),
                expiration,
            )
        except SchwabError as exc:
            if any(marker in str(exc) for marker in ("TooBigBody", "TooBig", "(502)")):
                last_error = exc
                continue
            raise
    raise last_error or SchwabError("Schwab option chain request failed")
