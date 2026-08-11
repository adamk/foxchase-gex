"""Local-only Schwab OAuth, chain retrieval, and payload minimization."""

from __future__ import annotations

import base64
import json
import math
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


load_dotenv()

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
MARKET_DATA_BASE = "https://api.schwabapi.com/marketdata/v1"
NY = ZoneInfo("America/New_York")


class SchwabError(RuntimeError):
    pass


def _config_dir() -> Path:
    configured = os.getenv("FOXCHASE_GEX_CONFIG_DIR", "~/.foxchase-gex")
    return Path(configured).expanduser()


def token_path() -> Path:
    configured = os.getenv("SCHWAB_TOKEN_PATH")
    return Path(configured).expanduser() if configured else _config_dir() / "schwab_tokens.json"


def _credentials() -> tuple[str, str, str]:
    client_id = os.getenv("SCHWAB_CLIENT_ID", "").strip()
    client_secret = os.getenv("SCHWAB_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1").strip()
    if not client_id or not client_secret:
        raise SchwabError(
            "SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET are required in .env"
        )
    return client_id, client_secret, redirect_uri


def authorization_url() -> str:
    client_id, _, redirect_uri = _credentials()
    return f"{AUTH_URL}?{urlencode({'client_id': client_id, 'redirect_uri': redirect_uri})}"


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def save_tokens(tokens: dict) -> None:
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
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


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


def exchange_authorization_response(value: str) -> dict:
    value = value.strip()
    if not value:
        raise SchwabError("authorization response is empty")
    if "://" in value:
        code = parse_qs(urlparse(value).query).get("code", [""])[0]
    else:
        code = value
    if not code:
        raise SchwabError("the pasted value did not contain an authorization code")

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
    return tokens


def _refresh_tokens(tokens: dict) -> dict:
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise SchwabError("refresh token is missing; run the login command again")
    client_id, client_secret, _ = _credentials()
    response = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    if not response.ok:
        raise SchwabError(
            f"Schwab token refresh failed ({response.status_code}): {response.text[:500]}"
        )
    refreshed = response.json()
    refreshed.setdefault("refresh_token", refresh_token)
    save_tokens(refreshed)
    return refreshed


def get_access_token() -> str:
    tokens = load_tokens()
    if not tokens:
        raise SchwabError("no local Schwab token found; run `python -m gex_client.login`")
    saved_at = int(tokens.get("saved_at", 0))
    expires_in = int(tokens.get("expires_in", 1800))
    if time.time() >= saved_at + expires_in - 90:
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
