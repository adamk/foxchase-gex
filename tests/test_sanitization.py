from gex_client.schwab import sanitize_chains


def test_sanitized_payload_contains_no_credentials_or_contract_symbols():
    expiration = "2026-08-10"
    call_chain = {
        "underlyingPrice": 7750.25,
        "access_token": "must-not-leak",
        "callExpDateMap": {
            f"{expiration}:0": {
                "7750.0": [{
                    "symbol": "SPXW SECRET CONTRACT",
                    "openInterest": 123,
                    "gamma": 0.004,
                    "volatility": 18.5,
                    "multiplier": 100,
                    "bid": 42.0,
                }]
            }
        },
    }
    put_chain = {
        "putExpDateMap": {
            f"{expiration}:0": {
                "7750.0": [{
                    "symbol": "SPXW OTHER SECRET",
                    "openInterest": 321,
                    "gamma": 0.005,
                    "volatility": 19.0,
                    "multiplier": 100,
                }]
            }
        }
    }

    payload = sanitize_chains("SPX", call_chain, put_chain, expiration)

    assert set(payload) == {"symbol", "spot", "expiration_date", "contracts"}
    assert set(payload["contracts"][0]) == {
        "option_type", "strike", "open_interest", "gamma", "volatility", "multiplier"
    }
    assert "token" not in repr(payload).lower()
    assert "secret" not in repr(payload).lower()
    assert len(payload["contracts"]) == 2

