"""Backup-local grant primitives for a private operator bridge."""
import hmac
import logging
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import schwab
from .auth_health import record_interactive_authorization, record_authenticated_success


_LOG = logging.getLogger(__name__)


class GrantRejected(RuntimeError):
    """Fixed diagnostic safe for the operator bridge to return."""


def prepare_grant():
    client_id, _, redirect = schwab._credentials()
    state = secrets.token_urlsafe(32)
    return state, schwab.AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": client_id,
        "redirect_uri": redirect, "state": state,
    })


def callback_code(value, redirect, state):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise GrantRejected("invalid_callback")
    try:
        actual, expected = urlsplit(value), urlsplit(redirect)
        pairs = parse_qsl(actual.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise GrantRejected("invalid_callback") from None
    if (actual.scheme, actual.netloc, actual.path or "/") != (
        expected.scheme, expected.netloc, expected.path or "/"
    ) or actual.fragment:
        raise GrantRejected("redirect_mismatch")
    params = dict(pairs)
    if len(params) != len(pairs) or "error" in params:
        raise GrantRejected("authorization_rejected")
    returned = params.get("state", "")
    if not returned.isascii() or not hmac.compare_digest(returned, state):
        raise GrantRejected("state_mismatch")
    code = params.get("code", "")
    if not code or len(code) > 4096:
        raise GrantRejected("invalid_code")
    return code


def exchange_and_validate(code):
    """Invoke only after the bridge durably consumes the owner-bound attempt."""
    client_id, secret, redirect = schwab._credentials()
    with schwab._token_store_lock():
        try:
            _LOG.warning("schwab_oauth stage=token_post_start")
            response = schwab.requests.post(
                schwab.TOKEN_URL,
                headers={"Authorization": schwab._basic_auth_header(client_id, secret)},
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
                timeout=30, allow_redirects=False,
            )
            _LOG.warning("schwab_oauth stage=token_post_response")
            if response.status_code != 200:
                raise GrantRejected("exchange_rejected")
            tokens = response.json()
            if not isinstance(tokens, dict) or any(
                not isinstance(tokens.get(key), str) or not tokens[key]
                for key in ("refresh_token", "access_token")
            ):
                raise GrantRejected("invalid_token_response")
            headers = {"Authorization": "Bearer " + tokens["access_token"]}
            _LOG.warning("schwab_oauth stage=chain_validation_start")
            for symbol in ("SPX", "NDX"):
                _LOG.warning("schwab_oauth stage=chain_request symbol=%s", symbol)
                validation = schwab.requests.get(
                    schwab.MARKET_DATA_BASE + "/chains",
                    headers=headers, params={"symbol": "$" + symbol, "strikeCount": 1},
                    timeout=15, allow_redirects=False,
                )
                if validation.status_code != 200:
                    raise GrantRejected("market_data_validation_failed")
                chain = validation.json()
                if not isinstance(chain, dict) or chain.get("symbol") != "$" + symbol or chain.get("status") != "SUCCESS":
                    raise GrantRejected("market_data_validation_failed")
                _LOG.warning("schwab_oauth stage=chain_validated symbol=%s", symbol)
        except (schwab.requests.RequestException, ValueError, TypeError) as exc:
            _LOG.warning(
                "schwab_oauth outcome=provider_response_unconfirmed exception_class=%s",
                type(exc).__name__,
            )
            raise GrantRejected("provider_response_unconfirmed") from None
        _LOG.warning("schwab_oauth stage=token_persist_start")
        schwab._save_tokens_unlocked(tokens)
        record_interactive_authorization()
        record_authenticated_success()
        _LOG.warning("schwab_oauth stage=authorization_committed")
