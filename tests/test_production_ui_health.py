import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import production_ui_health as health
from deploy.production_ui_health import DeploymentHealthError, evaluate_deployment_health


SPOT_ERROR = "Could not determine underlying spot price from Schwab response."
OAUTH_AT = "2026-10-10T22:48:24.842119+00:00"
DUE_AT = "2026-10-17T22:48:24.842119+00:00"
SATURDAY = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)
WEEKDAY = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)
HOLIDAY = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)


def empty_chain(symbol):
    return {
        "symbol": f"${symbol}",
        "callExpDateMap": {},
        "putExpDateMap": {},
    }


def no_session_response(symbol):
    return (
        500,
        {"error": SPOT_ERROR, "source": "schwab_internal", "symbol": symbol},
    )


def auth_status(now):
    last = now.astimezone(timezone.utc) - timedelta(hours=1)
    due = last + timedelta(days=7)
    return {
        "state": "healthy",
        "reauth_required": False,
        "last_full_oauth_at": last.isoformat(),
        "next_reauth_due_at": due.isoformat(),
        "remaining_seconds": int((due - now.astimezone(timezone.utc)).total_seconds()),
    }


def healthy_gate(**overrides):
    now = SATURDAY
    values = {
        "now": now,
        "service_active": True,
        "compute_active": True,
        "gunicorn_workers": 2,
        "restart_loop_clear": True,
        "site_status": 200,
        "backend_root_status": 200,
        "auth_status": auth_status(now),
        "quote_status": 200,
        "fresh_auth_failure": False,
        "assets_match": True,
        "gex_responses": {
            "NDX": no_session_response("NDX"),
            "SPX": no_session_response("SPX"),
        },
        "chain_responses": {
            symbol: [(200, empty_chain(symbol)), (200, empty_chain(symbol))]
            for symbol in ("NDX", "SPX")
        },
    }
    values.update(overrides)
    return evaluate_deployment_health(**values)


def test_weekend_no_0dte_gate_accepts_authenticated_empty_chains():
    assert healthy_gate() == "weekend_no_0dte_confirmed"


def test_sunday_no_0dte_gate_accepts_authenticated_empty_chains():
    assert healthy_gate(now=SUNDAY, auth_status=auth_status(SUNDAY)) == "weekend_no_0dte_confirmed"


def test_holiday_no_0dte_gate_accepts_authenticated_empty_chains():
    assert healthy_gate(
        now=HOLIDAY,
        auth_status=auth_status(HOLIDAY),
    ) == "market_holiday_no_0dte_confirmed"


def test_weekday_nonempty_gex_responses_pass():
    nonempty = {"symbol": "NDX", "strikes": [{"strike": 1, "gex": 2}]}
    assert healthy_gate(
        now=WEEKDAY,
        auth_status=auth_status(WEEKDAY),
        gex_responses={"NDX": (200, nonempty), "SPX": (200, {"strikes": [{}]})},
        chain_responses=None,
    ) == "weekday_gex_healthy"


def test_weekday_empty_chain_still_surfaces_as_data_problem():
    with pytest.raises(DeploymentHealthError, match="GEX API failed"):
        healthy_gate(
            now=WEEKDAY,
            auth_status=auth_status(WEEKDAY),
            gex_responses={"NDX": no_session_response("NDX"), "SPX": (200, {"strikes": [{}]})},
            chain_responses=None,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "invalid_grant", "symbol": "NDX"},
        {"error": "invalid_client", "symbol": "NDX"},
        {"error": "backend exception", "symbol": "NDX"},
        {"error": "compute failure", "symbol": "NDX"},
        {"error": "service unavailable", "symbol": "NDX"},
    ],
)
def test_weekend_hard_gex_failures_still_block(payload):
    with pytest.raises(DeploymentHealthError):
        healthy_gate(gex_responses={"NDX": (500, payload), "SPX": no_session_response("SPX")})


def test_weekend_nonempty_same_day_chain_does_not_get_misclassified():
    chain = empty_chain("NDX")
    chain["callExpDateMap"] = {"2026-10-10:0": {"25000": [{}]}}
    proof = {
        "NDX": [(200, chain), (200, empty_chain("NDX"))],
        "SPX": [(200, empty_chain("SPX")), (200, empty_chain("SPX"))],
    }
    with pytest.raises(DeploymentHealthError, match="same-day contracts"):
        healthy_gate(chain_responses=proof)


def test_weekend_chain_request_auth_failure_still_blocks():
    proof = {
        "NDX": [(401, {"error": "invalid_grant"}), (200, empty_chain("NDX"))],
        "SPX": [(200, empty_chain("SPX")), (200, empty_chain("SPX"))],
    }
    with pytest.raises(DeploymentHealthError, match="auth failure"):
        healthy_gate(chain_responses=proof)


def test_weekend_service_or_compute_failure_blocks():
    with pytest.raises(DeploymentHealthError, match="service is not active"):
        healthy_gate(compute_active=False)
    with pytest.raises(DeploymentHealthError, match="service is not active"):
        healthy_gate(service_active=False)


def test_wrong_gunicorn_worker_count_blocks():
    with pytest.raises(DeploymentHealthError, match="Gunicorn worker"):
        healthy_gate(gunicorn_workers=1)


def test_service_restart_during_gate_blocks():
    with pytest.raises(DeploymentHealthError, match="restarted"):
        healthy_gate(restart_loop_clear=False)


def test_weekend_stale_or_latched_auth_blocks():
    status = auth_status(SATURDAY)
    status["reauth_required"] = True
    status["state"] = "reauth_required"
    with pytest.raises(DeploymentHealthError, match="lifecycle"):
        healthy_gate(auth_status=status)


def test_weekend_invalid_grant_in_fresh_logs_blocks():
    with pytest.raises(DeploymentHealthError, match="fresh logs"):
        healthy_gate(fresh_auth_failure=True)


def test_weekend_authenticated_schwab_probe_failure_blocks():
    with pytest.raises(DeploymentHealthError, match="probe failed"):
        healthy_gate(quote_status=401)


def test_static_asset_hash_mismatch_blocks():
    with pytest.raises(DeploymentHealthError, match="asset hash"):
        healthy_gate(assets_match=False)


def test_weekend_run_checks_roots_assets_auth_and_empty_0dte(monkeypatch):
    oauth_epoch = datetime.fromisoformat(OAUTH_AT).timestamp()
    now = datetime.fromtimestamp(oauth_epoch + 100, timezone.utc)
    status = {
        "state": "healthy",
        "reauth_required": False,
        "last_full_oauth_at": OAUTH_AT,
        "next_reauth_due_at": DUE_AT,
        "remaining_seconds": 604700,
    }
    token = {"access_token": "test-access"}
    chain_probes = []
    visited = []
    monkeypatch.setattr(health, "_service_is_active", lambda service: True)
    monkeypatch.setattr(health, "_read_service_snapshot", lambda service: ("active", 123, 0))
    monkeypatch.setattr(health, "_gunicorn_worker_count", lambda main_pid: 2)

    def require_200(url, description):
        visited.append((url, description))
        return b"ok"

    monkeypatch.setattr(health, "_require_200", require_200)
    monkeypatch.setattr(health, "_check_asset_hashes", lambda *args: True)
    monkeypatch.setattr(health, "_check_auth_state", lambda *args: (status, token))
    monkeypatch.setattr(health, "_probe_authenticated_quotes", lambda value: 200)
    monkeypatch.setattr(health, "_check_fresh_logs", lambda *args: False)
    monkeypatch.setattr(
        health,
        "_read_json_response",
        lambda url, headers=None: no_session_response(url.rsplit("/", 1)[-1]),
    )

    def probe_chains(access_token, symbol, local_day):
        chain_probes.append((access_token, symbol, local_day))
        return [(200, empty_chain(symbol)), (200, empty_chain(symbol))]

    monkeypatch.setattr(health, "_probe_same_day_chains", probe_chains)

    result = health.run_health_check(
        site_url="https://example.test/",
        api_root="https://example.test/api/gex",
        local_url="http://127.0.0.1:5055/",
        token_path=Path("/unused/token.json"),
        now=now,
    )

    assert result == "weekend_no_0dte_confirmed"
    assert [symbol for _, symbol, _ in chain_probes] == ["NDX", "SPX"]
    assert {description for _, description in visited} >= {
        "production root", "local backend root", "production CSS", "production JavaScript"
    }
    assert not any(url.rstrip("/").endswith("/api/health") for url, _ in visited)


def test_gunicorn_worker_count_counts_service_master_children(monkeypatch):
    output = """\
100 1 /usr/bin/python -m gunicorn --workers 2 --bind 127.0.0.1:5055 wsgi:app
101 100 /usr/bin/python -m gunicorn --workers 2 --bind 127.0.0.1:5055 wsgi:app
102 100 /usr/bin/python -m gunicorn --workers 2 --bind 127.0.0.1:5055 wsgi:app
103 100 /usr/bin/python helper
104 999 /usr/bin/python -m gunicorn --workers 2 wsgi:app
"""
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    assert health._gunicorn_worker_count(100) == 2


def test_service_snapshot_reads_labeled_systemd_properties(monkeypatch):
    output = "NRestarts=0\nMainPID=123\nActiveState=active\n"
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    assert health._read_service_snapshot("foxchase-gex.service") == ("active", 123, 0)


def test_invalid_grant_api_response_blocks_integrated_deployment_gate(monkeypatch):
    now = datetime.fromisoformat(OAUTH_AT).replace(tzinfo=timezone.utc) + timedelta(seconds=100)
    auth = auth_status(now)
    with pytest.raises(DeploymentHealthError, match="authentication failure"):
        evaluate_deployment_health(
            now=now,
            service_active=True,
            compute_active=True,
            gunicorn_workers=2,
            restart_loop_clear=True,
            site_status=200,
            backend_root_status=200,
            auth_status=auth,
            quote_status=200,
            fresh_auth_failure=False,
            assets_match=True,
            gex_responses={
                "NDX": (500, {"error": "invalid_grant"}),
                "SPX": no_session_response("SPX"),
            },
            chain_responses={
                "NDX": [(200, empty_chain("NDX")), (200, empty_chain("NDX"))],
                "SPX": [(200, empty_chain("SPX")), (200, empty_chain("SPX"))],
            },
        )


def test_auth_gate_reads_operator_lifecycle_and_restores_environment(monkeypatch, tmp_path):
    oauth_epoch = datetime.fromisoformat(OAUTH_AT).timestamp()
    now = oauth_epoch + 100
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "schwab_lifecycle.py").write_text(
        "def operator_status(now):\n"
        "    return {\n"
        "        'state': 'healthy',\n"
        "        'reauth_required': False,\n"
        f"        'last_full_oauth_at': '{OAUTH_AT}',\n"
        f"        'next_reauth_due_at': '{DUE_AT}',\n"
        "        'remaining_seconds': 604700,\n"
        "    }\n",
        encoding="utf-8",
    )
    token_path = tmp_path / "schwab_tokens.json"
    token_path.write_text(
        json.dumps({"access_token": "test", "saved_at": int(now), "expires_in": 1800}),
        encoding="utf-8",
    )
    token_path.chmod(0o600)
    monkeypatch.setattr(health, "GEX_ROOT", tmp_path)
    monkeypatch.setattr(health.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="foxchase-gex"))
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", "previous-token-path")

    auth, saved_token = health._check_auth_state(token_path, now)

    assert auth["last_full_oauth_at"] == OAUTH_AT
    assert saved_token["access_token"] == "test"
    assert os.environ["SCHWAB_TOKEN_PATH"] == "previous-token-path"


def test_fresh_invalid_grant_log_blocks_health_gate(monkeypatch):
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="invalid_grant"),
    )

    assert health._check_fresh_logs(OAUTH_AT, "foxchase-gex.service") is True
