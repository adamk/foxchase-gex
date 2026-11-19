from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from gex_client import schwab_browser_grant as grant


@pytest.mark.parametrize("url,error", [
    ("https://evil.test/?code=test&state=known", "redirect_mismatch"),
    ("https://127.0.0.1?code=test&state=wrong", "state_mismatch"),
    ("https://127.0.0.1?error=access_denied&state=known", "authorization_rejected"),
    ("https://127.0.0.1?code=a&code=b&state=known", "authorization_rejected"),
])
def test_callback_failure(url, error):
    with pytest.raises(grant.GrantRejected, match=error):
        grant.callback_code(url, "https://127.0.0.1", "known")


@pytest.mark.parametrize("case", ["success", "invalid", "interrupted", "invalid_ndx"])
def test_save_only_after_both_symbols_validate(monkeypatch, case):
    s = grant.schwab
    monkeypatch.setattr(s, "_credentials", lambda: ("id", "secret", "https://127.0.0.1"))
    monkeypatch.setattr(s, "_token_store_lock", nullcontext)
    save = Mock()
    monkeypatch.setattr(s, "_save_tokens_unlocked", save)
    health = Mock()
    monkeypatch.setattr(grant, "record_interactive_authorization", health)
    monkeypatch.setattr(grant, "record_authenticated_success", Mock())
    tokens = {"access_token": "mock-access", "refresh_token": "mock-refresh"}
    post = Mock(return_value=SimpleNamespace(status_code=200 if case != "invalid" else 400, json=lambda: tokens))
    if case == "interrupted":
        post.side_effect = s.requests.Timeout("sensitive mock detail")
    monkeypatch.setattr(s.requests, "post", post)
    def chains(*args, **kw):
        symbol = kw["params"]["symbol"]
        return SimpleNamespace(status_code=401 if case == "invalid_ndx" and symbol == "$NDX" else 200,
                               json=lambda: {"symbol": symbol, "status": "SUCCESS"})
    monkeypatch.setattr(s.requests, "get", chains)
    if case == "success":
        assert grant.callback_code("https://127.0.0.1?code=test&state=known", "https://127.0.0.1", "known") == "test"
        grant.exchange_and_validate("test")
        save.assert_called_once_with(tokens)
        health.assert_called_once()
    else:
        with pytest.raises(grant.GrantRejected) as error:
            grant.exchange_and_validate("test")
        assert "sensitive mock detail" not in str(error.value)
        save.assert_not_called()
        health.assert_not_called()
