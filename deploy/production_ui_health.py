#!/usr/bin/env python3
"""Production health gate for the hosted GEX UI release."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


LIFETIME_SECONDS = 7 * 24 * 60 * 60
WARNING_SECONDS = 6 * 24 * 60 * 60
SPOT_ERROR = "Could not determine underlying spot price from Schwab response."
TOKEN_PATH = Path(
    "/opt/foxchase-authority-runtime/rootfs/var/lib/foxchase-gex/schwab_tokens.json"
)
GEX_ROOT = Path("/opt/foxchase-authority-runtime/rootfs/opt/foxchase-gex")
SERVICE_PYTHON = Path("/opt/foxchase-authority-runtime/gex-venv/bin/python")
SERVICE_ACCOUNT = "foxchase-gex"
SERVICE_EXTRA_GROUPS = ("ubuntu", "foxchase-dashboard")
STATIC_ROOT = GEX_ROOT / "static"
SERVICE = "foxchase-gex.service"
COMPUTE_SERVICE = "foxchase-gex-compute.service"
EXPECTED_GUNICORN_WORKERS = 2
SCHWAB_MARKET_DATA = "https://api.schwabapi.com/marketdata/v1"
NY = ZoneInfo("America/New_York")
ASSET_PATHS = {
    "index.html": Path("templates/index.html"),
    "app.js": Path("js/app.js"),
    "style.css": Path("css/style.css"),
}


class DeploymentHealthError(RuntimeError):
    """A production health condition that must block or roll back deployment."""


def _parse_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DeploymentHealthError("response was not UTF-8") from exc
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise DeploymentHealthError("response was not valid JSON") from exc
        if isinstance(parsed, dict):
            return parsed
    raise DeploymentHealthError("response was not a JSON object")


def _auth_failure(payload: dict) -> bool:
    encoded = json.dumps(payload, sort_keys=True).lower()
    return any(marker in encoded for marker in ("invalid_grant", "invalid_client"))


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last_day = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - weekday) % 7)


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == 5:
        return actual - timedelta(days=1)
    if actual.weekday() == 6:
        return actual + timedelta(days=1)
    return actual


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _is_us_equity_market_closed(day: date) -> bool:
    if day.weekday() >= 5:
        return True
    holidays = set()
    for year in (day.year - 1, day.year, day.year + 1):
        holidays.update({
            _observed_fixed_holiday(year, 1, 1),
            _nth_weekday(year, 1, 0, 3),
            _nth_weekday(year, 2, 0, 3),
            _easter_sunday(year) - timedelta(days=2),
            _last_weekday(year, 5, 0),
            _observed_fixed_holiday(year, 7, 4),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 11, 3, 4),
            _observed_fixed_holiday(year, 12, 25),
        })
        if year >= 2022:
            holidays.add(_observed_fixed_holiday(year, 6, 19))
    return day in holidays


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone missing")
    return parsed.astimezone(timezone.utc)


def _validate_lifecycle(status: dict, now: datetime) -> None:
    if (
        not isinstance(status, dict)
        or status.get("state") != "healthy"
        or status.get("reauth_required") is not False
    ):
        raise DeploymentHealthError("operator Schwab auth lifecycle is not healthy")
    try:
        last = _parse_utc(status["last_full_oauth_at"])
        due = _parse_utc(status["next_reauth_due_at"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DeploymentHealthError("operator Schwab auth lifecycle timestamps are invalid") from exc
    now_utc = now.astimezone(timezone.utc)
    age = (now_utc - last).total_seconds()
    remaining = status.get("remaining_seconds")
    if (
        last > now_utc
        or due <= now_utc
        or abs((due - last).total_seconds() - LIFETIME_SECONDS) > 1
        or age >= WARNING_SECONDS
        or isinstance(remaining, bool)
        or not isinstance(remaining, (int, float))
        or remaining <= 0
        or abs(float(remaining) - (due - now_utc).total_seconds()) > 1
    ):
        raise DeploymentHealthError("operator Schwab auth lifecycle is not healthy")


def _has_contracts(groups: dict) -> bool:
    def contains(value):
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return bool(value)
        return bool(value)

    return contains(groups)


def _validate_empty_same_day_chains(symbol: str, responses) -> None:
    if not isinstance(responses, (list, tuple)) or len(responses) != 2:
        raise DeploymentHealthError(f"{symbol} no-session state lacks authenticated chain proof")
    for status_code, body in responses:
        payload = _parse_object(body)
        if _auth_failure(payload):
            raise DeploymentHealthError("authenticated Schwab chain reported an auth failure")
        if int(status_code) != 200:
            raise DeploymentHealthError("authenticated Schwab chain request failed")
        returned_symbol = payload.get("symbol")
        if returned_symbol is not None and str(returned_symbol).upper() != f"${symbol}":
            raise DeploymentHealthError(f"{symbol} Schwab chain symbol did not match")
        for key in ("callExpDateMap", "putExpDateMap"):
            groups = payload.get(key)
            if not isinstance(groups, dict):
                raise DeploymentHealthError(f"{symbol} Schwab chain expiry groups are missing")
            if _has_contracts(groups):
                raise DeploymentHealthError(f"{symbol} Schwab chain contains same-day contracts")


def _validate_gex_responses(
    responses: dict,
    *,
    market_closed: bool,
    chain_responses: dict | None,
) -> bool:
    if not isinstance(responses, dict) or set(responses) != {"NDX", "SPX"}:
        raise DeploymentHealthError("NDX/SPX GEX responses are incomplete")
    no_session = False
    for symbol in ("NDX", "SPX"):
        status_code, body = responses[symbol]
        payload = _parse_object(body)
        if _auth_failure(payload):
            raise DeploymentHealthError("GEX API reported a Schwab authentication failure")
        if 200 <= int(status_code) < 300 and payload and "error" not in payload:
            continue
        if (
            not market_closed
            or payload.get("error") != SPOT_ERROR
            or str(payload.get("symbol", "")).upper() != symbol
        ):
            raise DeploymentHealthError(f"{symbol} GEX API failed ({status_code})")
        chains = chain_responses.get(symbol) if isinstance(chain_responses, dict) else None
        _validate_empty_same_day_chains(symbol, chains)
        no_session = True
    return no_session


def evaluate_deployment_health(
    *,
    now: datetime,
    service_active: bool,
    compute_active: bool,
    gunicorn_workers: int,
    restart_loop_clear: bool,
    site_status: int,
    backend_root_status: int,
    auth_status: dict,
    quote_status: int,
    fresh_auth_failure: bool,
    assets_match: bool,
    gex_responses: dict,
    chain_responses: dict | None = None,
) -> str:
    """Apply the deployment policy to sanitized observations from the host."""
    if not service_active or not compute_active:
        raise DeploymentHealthError("GEX or compute service is not active")
    if gunicorn_workers != EXPECTED_GUNICORN_WORKERS:
        raise DeploymentHealthError(
            f"unexpected Gunicorn worker count ({gunicorn_workers}; expected {EXPECTED_GUNICORN_WORKERS})"
        )
    if not restart_loop_clear:
        raise DeploymentHealthError("GEX service restarted during the health gate")
    if site_status != 200:
        raise DeploymentHealthError("production root did not return HTTP 200")
    if backend_root_status != 200:
        raise DeploymentHealthError("local backend root did not return HTTP 200")
    if not assets_match:
        raise DeploymentHealthError("production UI asset hash mismatch")
    _validate_lifecycle(auth_status, now)
    if quote_status != 200:
        raise DeploymentHealthError("authenticated Schwab probe failed")
    if fresh_auth_failure:
        raise DeploymentHealthError("fresh logs contain invalid_grant or invalid_client")

    local_day = now.astimezone(NY).date()
    if _is_us_equity_market_closed(local_day):
        no_session = _validate_gex_responses(
            gex_responses,
            market_closed=True,
            chain_responses=chain_responses,
        )
        if local_day.weekday() >= 5:
            return "weekend_no_0dte_confirmed" if no_session else "weekend_gex_healthy"
        return "market_holiday_no_0dte_confirmed" if no_session else "market_holiday_gex_healthy"
    _validate_gex_responses(
        gex_responses,
        market_closed=False,
        chain_responses=None,
    )
    return "weekday_gex_healthy"


def _read_response(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    request = Request(url, headers=headers or {"User-Agent": "Foxchase-UI-Health/1"})
    try:
        with urlopen(request, timeout=25) as response:
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()
    except (TimeoutError, URLError, OSError) as exc:
        raise DeploymentHealthError("production HTTP request failed") from exc


def _read_json_response(url: str, headers: dict | None = None) -> tuple[int, dict]:
    status, body = _read_response(url, headers)
    return status, _parse_object(body)


def _require_200(url: str, description: str) -> bytes:
    status, body = _read_response(url)
    if status != 200 or not body:
        raise DeploymentHealthError(f"{description} did not return nonempty HTTP 200")
    return body


def _service_is_active(service: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", service],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _read_service_snapshot(service: str) -> tuple[str, int, int]:
    result = subprocess.run(
        [
            "systemctl", "show", service,
            "--property=ActiveState,MainPID,NRestarts",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DeploymentHealthError("GEX service runtime state could not be checked")
    try:
        fields = dict(
            line.split("=", maxsplit=1)
            for line in result.stdout.splitlines()
            if "=" in line
        )
        return fields["ActiveState"], int(fields["MainPID"]), int(fields["NRestarts"])
    except (KeyError, ValueError) as exc:
        raise DeploymentHealthError("GEX service runtime state was malformed") from exc


def _gunicorn_worker_count(main_pid: int) -> int:
    if main_pid <= 0:
        return 0
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,args="],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DeploymentHealthError("Gunicorn worker processes could not be checked")
    workers = 0
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        try:
            parent_pid = int(fields[1])
        except ValueError:
            continue
        # On the deployed Gunicorn build, workers retain the master's argv;
        # direct Gunicorn children of the systemd MainPID are the workers.
        if parent_pid == main_pid and "gunicorn" in fields[2]:
            workers += 1
    return workers


def _check_asset_hashes(asset_root: Path, manifest_path: Path) -> bool:
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
        expected = {}
        for line in lines:
            if not line.strip():
                continue
            digest, name = line.split(maxsplit=1)
            filename = Path(name.strip()).name
            if filename in expected or filename not in ASSET_PATHS:
                raise DeploymentHealthError("asset hash manifest is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise DeploymentHealthError("asset hash manifest is invalid")
            expected[filename] = digest
        if set(expected) != set(ASSET_PATHS):
            raise DeploymentHealthError("asset hash manifest is incomplete")
        for filename, relative in ASSET_PATHS.items():
            content = (asset_root / relative).read_bytes()
            if hashlib.sha256(content).hexdigest() != expected[filename]:
                return False
    except DeploymentHealthError:
        raise
    except (OSError, ValueError) as exc:
        raise DeploymentHealthError("production UI asset hashes could not be checked") from exc
    return True


def _read_secure_token(token_path: Path) -> dict:
    try:
        token_stat = token_path.stat()
        owner = pwd.getpwuid(token_stat.st_uid).pw_name
        token = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DeploymentHealthError("Schwab auth state could not be read") from exc

    if (
        not isinstance(token, dict)
        or owner != "foxchase-gex"
        or stat.S_IMODE(token_stat.st_mode) != 0o600
    ):
        raise DeploymentHealthError("Schwab token ownership or mode is invalid")
    return token


def _access_token_is_fresh(token: dict, now: float) -> bool:
    try:
        saved_at = int(token.get("saved_at", 0))
        expires_in = int(token.get("expires_in", 1800))
        refresh_after_age = max(60, expires_in - 120)
        return bool(token.get("access_token")) and now - saved_at <= refresh_after_age
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def _refresh_access_token_as_service(token_path: Path) -> None:
    command = [
        str(SERVICE_PYTHON),
        "-c",
        "from app.schwab_auth import get_access_token; get_access_token()",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            cwd=str(GEX_ROOT),
            env={
                "PATH": os.defpath,
                "PYTHONPATH": str(GEX_ROOT),
                "SCHWAB_TOKEN_PATH": str(token_path),
            },
            user=SERVICE_ACCOUNT,
            group=SERVICE_ACCOUNT,
            extra_groups=SERVICE_EXTRA_GROUPS,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise DeploymentHealthError(
            "production Schwab access-token refresh could not run"
        ) from exc
    if result.returncode != 0:
        raise DeploymentHealthError("production Schwab access-token refresh failed")


def _check_auth_state(token_path: Path, now: float) -> tuple[dict, dict]:
    token = _read_secure_token(token_path)

    try:
        lifecycle_source = GEX_ROOT / "app" / "schwab_lifecycle.py"
        spec = importlib.util.spec_from_file_location(
            "foxchase_production_schwab_lifecycle", lifecycle_source
        )
        if spec is None or spec.loader is None:
            raise ImportError("production lifecycle module unavailable")
        lifecycle_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lifecycle_module)
    except (OSError, ImportError, AttributeError, TypeError, ValueError) as exc:
        raise DeploymentHealthError("production operator auth status could not be read") from exc

    def read_status() -> dict:
        prior_token_path = os.environ.get("SCHWAB_TOKEN_PATH")
        os.environ["SCHWAB_TOKEN_PATH"] = str(token_path)
        try:
            return lifecycle_module.operator_status(now)
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            raise DeploymentHealthError(
                "production operator auth status could not be read"
            ) from exc
        finally:
            if prior_token_path is None:
                os.environ.pop("SCHWAB_TOKEN_PATH", None)
            else:
                os.environ["SCHWAB_TOKEN_PATH"] = prior_token_path

    status = read_status()
    now_utc = datetime.fromtimestamp(now, timezone.utc)
    _validate_lifecycle(status, now_utc)
    if not _access_token_is_fresh(token, now):
        _refresh_access_token_as_service(token_path)
        token = _read_secure_token(token_path)
        if not _access_token_is_fresh(token, now):
            raise DeploymentHealthError("Schwab access token is missing or stale")
        refreshed_status = read_status()
        _validate_lifecycle(refreshed_status, now_utc)
        if any(
            refreshed_status.get(field) != status.get(field)
            for field in ("last_full_oauth_at", "next_reauth_due_at", "reauth_required")
        ):
            raise DeploymentHealthError("normal token refresh changed OAuth lifecycle")
        status = refreshed_status
    return status, token


def _check_fresh_logs(since: str, service: str) -> bool:
    result = subprocess.run(
        ["journalctl", "--unit", service, "--since", since, "--no-pager", "--output=cat"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DeploymentHealthError("fresh service logs could not be checked")
    return bool(re.search(r"invalid_grant|invalid_client", result.stdout, re.IGNORECASE))


def _probe_authenticated_quotes(token: str) -> int:
    url = f"{SCHWAB_MARKET_DATA}/quotes?{urlencode({'symbols': '$SPX'})}"
    status, body = _read_response(
        url,
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if status == 200:
        try:
            payload = _parse_object(body)
        except (TypeError, ValueError) as exc:
            raise DeploymentHealthError("authenticated Schwab probe returned invalid JSON") from exc
        if _auth_failure(payload) or "error" in payload:
            raise DeploymentHealthError("authenticated Schwab probe returned an API error")
    return status


def _probe_same_day_chains(token: str, symbol: str, local_day: date) -> list[tuple[int, dict]]:
    schwab_symbol = f"${symbol.upper()}"
    strike_count = 40 if symbol.upper() == "SPX" else 100
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    results = []
    for contract_type in ("CALL", "PUT"):
        query = urlencode({
            "symbol": schwab_symbol,
            "contractType": contract_type,
            "strategy": "SINGLE",
            "strikeCount": strike_count,
            "fromDate": local_day.isoformat(),
            "toDate": local_day.isoformat(),
        })
        results.append(
            _read_json_response(f"{SCHWAB_MARKET_DATA}/chains?{query}", headers)
        )
    return results


def run_health_check(
    *,
    site_url: str,
    api_root: str,
    local_url: str,
    token_path: Path = TOKEN_PATH,
    asset_root: Path = STATIC_ROOT,
    asset_manifest: Path | None = None,
    service: str = SERVICE,
    compute_service: str = COMPUTE_SERVICE,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    manifest = asset_manifest or Path(__file__).parent / "production-ui" / "SHA256SUMS"
    service_before = _read_service_snapshot(service)
    service_active = service_before[0] == "active" and service_before[1] > 0
    compute_active = _service_is_active(compute_service)
    if not service_active or not compute_active:
        raise DeploymentHealthError("GEX or compute service is not active")
    workers_before = _gunicorn_worker_count(service_before[1])
    if workers_before != EXPECTED_GUNICORN_WORKERS:
        raise DeploymentHealthError(
            f"unexpected Gunicorn worker count ({workers_before}; expected {EXPECTED_GUNICORN_WORKERS})"
        )
    site_body = _require_200(site_url, "production root")
    backend_root = _require_200(local_url, "local backend root")
    _require_200(urljoin(site_url, "/static/css/style.css"), "production CSS")
    _require_200(urljoin(site_url, "/static/js/app.js"), "production JavaScript")
    assets_match = _check_asset_hashes(asset_root, manifest)
    if not assets_match:
        raise DeploymentHealthError("production UI asset hash mismatch")

    auth_status, token = _check_auth_state(token_path, now.timestamp())
    _validate_lifecycle(auth_status, now)
    quote_status = _probe_authenticated_quotes(token["access_token"])
    if quote_status != 200:
        raise DeploymentHealthError("authenticated Schwab probe failed")
    fresh_auth_failure = _check_fresh_logs(auth_status["last_full_oauth_at"], service)
    if fresh_auth_failure:
        raise DeploymentHealthError("fresh logs contain invalid_grant or invalid_client")

    local_day = now.astimezone(NY).date()
    market_closed = _is_us_equity_market_closed(local_day)
    gex_responses = {
        symbol: _read_json_response(f"{api_root}/{symbol}")
        for symbol in ("NDX", "SPX")
    }
    chain_responses = {}
    if market_closed:
        for symbol, (status_code, payload) in gex_responses.items():
            if (
                status_code >= 400
                and payload.get("error") == SPOT_ERROR
                and str(payload.get("symbol", "")).upper() == symbol
            ):
                chain_responses[symbol] = _probe_same_day_chains(
                    token["access_token"], symbol, local_day
                )

    service_after = _read_service_snapshot(service)
    compute_active = compute_active and _service_is_active(compute_service)
    workers_after = _gunicorn_worker_count(service_after[1])
    restart_loop_clear = (
        service_after[0] == "active"
        and service_before[1:] == service_after[1:]
    )
    gunicorn_workers = workers_after if workers_before == workers_after else -1

    result = evaluate_deployment_health(
        now=now,
        service_active=service_active,
        compute_active=compute_active,
        gunicorn_workers=gunicorn_workers,
        restart_loop_clear=restart_loop_clear,
        site_status=200 if site_body else 0,
        backend_root_status=200 if backend_root else 0,
        auth_status=auth_status,
        quote_status=quote_status,
        fresh_auth_failure=fresh_auth_failure,
        assets_match=assets_match,
        gex_responses=gex_responses,
        chain_responses=chain_responses,
    )
    if result == "weekend_no_0dte_confirmed":
        print("DEPLOYMENT HEALTH PASS: auth and services healthy; weekend empty 0DTE chains confirmed")
    elif result == "market_holiday_no_0dte_confirmed":
        print("DEPLOYMENT HEALTH PASS: auth and services healthy; market-holiday empty 0DTE chains confirmed")
    elif result in {"weekend_gex_healthy", "market_holiday_gex_healthy"}:
        print("DEPLOYMENT HEALTH PASS: auth and services healthy; closed-day GEX responses valid")
    else:
        print("DEPLOYMENT HEALTH PASS: service, two Gunicorn workers, roots, assets, Schwab auth, and weekday NDX/SPX GEX APIs healthy")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-url", default="https://gex.foxchasetrading.com")
    parser.add_argument("--api-root", default="https://gex.foxchasetrading.com/api/gex")
    parser.add_argument("--local-url", default="http://127.0.0.1:5055/")
    parser.add_argument("--token-path", type=Path, default=TOKEN_PATH)
    parser.add_argument("--asset-root", type=Path, default=STATIC_ROOT)
    parser.add_argument("--asset-manifest", type=Path)
    parser.add_argument("--service", default=SERVICE)
    parser.add_argument("--compute-service", default=COMPUTE_SERVICE)
    args = parser.parse_args(argv)
    try:
        run_health_check(
            site_url=args.site_url,
            api_root=args.api_root,
            local_url=args.local_url,
            token_path=args.token_path,
            asset_root=args.asset_root,
            asset_manifest=args.asset_manifest,
            service=args.service,
            compute_service=args.compute_service,
        )
    except DeploymentHealthError as exc:
        print(f"DEPLOYMENT HEALTH FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
