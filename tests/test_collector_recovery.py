"""Offline regression checks for terminal OAuth recovery behavior."""

import sys
from datetime import datetime as RealDateTime
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")


def test_terminal_oauth_recovery_reprobes_and_resumes_collection(monkeypatch):
    from gex_client import collector

    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            value = RealDateTime(2026, 11, 4, 8, 45, tzinfo=NY)
            return value.astimezone(tz) if tz else value

    calls = {"token": 0, "recovery_notice": 0, "symbols": []}
    monkeypatch.setattr(collector, "datetime", FixedDateTime)
    monkeypatch.setattr(collector.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(collector.time, "sleep", lambda _seconds: (_ for _ in ()).throw(AssertionError("unexpected sleep")))
    monkeypatch.setattr(collector, "authorization_state", lambda _state: {"health": "reauthorization_required"})
    monkeypatch.setattr(collector, "load_status", lambda: {"alerts_sent": {"required": True}})
    monkeypatch.setattr(collector, "send_dashboard_status", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(collector, "get_access_token", lambda: calls.__setitem__("token", calls["token"] + 1) or "synthetic-token")
    monkeypatch.setattr(collector, "emit_due_alert", lambda: None)
    monkeypatch.setattr(collector, "emit_recovery_if_pending", lambda: calls.__setitem__("recovery_notice", calls["recovery_notice"] + 1) or True)
    monkeypatch.setattr(collector, "collect_once", lambda _base, symbol, _session: calls["symbols"].append(symbol))
    monkeypatch.setattr(collector, "record_attempt", lambda *_args, **_kwargs: {"alert": None})
    monkeypatch.setattr(sys, "argv", ["collector", "--once"])

    collector.main()

    assert calls["token"] == 1
    assert calls["recovery_notice"] == 1
    assert calls["symbols"] == ["SPX", "NDX"]
