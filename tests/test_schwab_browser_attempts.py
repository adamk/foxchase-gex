from unittest.mock import Mock

import pytest

from gex_client import schwab_browser_attempts as m

OWNER = "a" * 64
URL = "https://127.0.0.1?code=sample&state=state"


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    if m.sys.platform != "linux":
        pytest.skip("The Schwab OAuth attempt store requires Linux and POSIX file safeguards")
    monkeypatch.setattr(m, "prepare_grant", lambda: ("state", "https://provider.test"))
    monkeypatch.setattr(m.schwab, "_credentials", lambda: ("id", "secret", "https://127.0.0.1"))
    exchange = Mock()
    monkeypatch.setattr(m, "exchange_and_validate", exchange)
    return m.Attempts(tmp_path / "attempt.db"), exchange


def test_unsupported_platform_rejected_before_creating_store(tmp_path, monkeypatch):
    path = tmp_path / "attempt.db"
    monkeypatch.setattr(m.sys, "platform", "win32")
    with pytest.raises(NotImplementedError, match="requires Linux"):
        m.Attempts(path)
    assert not path.exists()


def test_owner_and_single_use(attempt):
    store, exchange = attempt
    store.begin(OWNER)
    for operation in (lambda: store.begin("b" * 64), lambda: store.complete("b" * 64, URL)):
        with pytest.raises(m.GrantRejected):
            operation()
    store.complete(OWNER, URL)
    with pytest.raises(m.GrantRejected):
        store.complete(OWNER, URL)
    exchange.assert_called_once_with("sample")


def test_expired_attempt(attempt, monkeypatch):
    store, exchange = attempt
    monkeypatch.setattr(m.time, "time", lambda: 1)
    store.begin(OWNER)
    monkeypatch.setattr(m.time, "time", lambda: 1 + m.TTL_SECONDS)
    with pytest.raises(m.GrantRejected, match="attempt_unavailable"):
        store.complete(OWNER, URL)
    exchange.assert_not_called()


def test_ambiguous_exchange_fails_closed(attempt):
    store, exchange = attempt
    exchange.side_effect = m.GrantRejected("provider_response_unconfirmed")
    store.begin(OWNER)
    with pytest.raises(m.GrantRejected, match="provider_response_unconfirmed"):
        store.complete(OWNER, URL)
    with pytest.raises(m.GrantRejected, match="attempt_busy"):
        store.begin(OWNER)
    with pytest.raises(m.GrantRejected, match="attempt_unavailable"):
        store.complete(OWNER, URL)
    exchange.assert_called_once()


def test_concurrent_completions_exchange_once(attempt, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store, _ = attempt
    entered, release = Event(), Event()

    def exchange(code):
        entered.set()
        assert release.wait(5)

    mock = Mock(side_effect=exchange)
    monkeypatch.setattr(m, "exchange_and_validate", mock)
    store.begin(OWNER)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(store.complete, OWNER, URL)
        try:
            assert entered.wait(5)
            with pytest.raises(m.GrantRejected, match="attempt_unavailable"):
                store.complete(OWNER, URL)
            with pytest.raises(m.GrantRejected, match="attempt_busy"):
                store.begin(OWNER)
        finally:
            release.set()
        first.result()
    mock.assert_called_once_with("sample")


@pytest.mark.parametrize("failure,expected,final_state", [
    (m.GrantRejected("provider_response_unconfirmed"), "provider_response_unconfirmed", "exchanging"),
    (m.GrantRejected("mock provider returned sensitive data"), "other_rejection", "failed"),
    (RuntimeError("mock unexpected sensitive data"), "unexpected_exception", "exchanging"),
])
def test_safe_outcome_logging(attempt, caplog, failure, expected, final_state):
    import sqlite3

    store, exchange = attempt
    caplog.set_level("WARNING")
    exchange.side_effect = failure
    store.begin(OWNER)
    with pytest.raises(type(failure)):
        store.complete(OWNER, URL)

    assert "schwab_oauth outcome=" + expected in caplog.text
    assert "mock provider returned sensitive data" not in caplog.text
    assert "mock unexpected sensitive data" not in caplog.text
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT status FROM attempt WHERE slot=1").fetchone()[0] == final_state


def test_success_logs_only_after_commit(attempt, caplog):
    store, exchange = attempt
    caplog.set_level("WARNING")
    store.begin(OWNER)
    store.complete(OWNER, URL)
    exchange.assert_called_once_with("sample")
    assert "schwab_oauth outcome=succeeded" in caplog.text
