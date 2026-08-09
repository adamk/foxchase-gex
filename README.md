# Foxchase GEX Community Client

This is the open-source, self-run client for the Foxchase SPX/NDX 0DTE gamma-exposure dashboard. Your Schwab app credentials and OAuth tokens stay on your computer. The client retrieves the option chain locally, removes every field the calculation does not need, and sends the minimized numeric snapshot to the private Foxchase calculation API.

The GEX calculation and Foxchase Read classifier are intentionally not included in this repository. The community client is free to run yourself; a fully hosted version may be offered separately after the required brokerage/data approvals are in place.

## Data boundary

The EC2 request contains only:

- symbol and underlying spot;
- current 0DTE expiration date;
- option type, strike, open interest, gamma, volatility fallback, and multiplier.

It does **not** contain your Schwab client ID, client secret, access token, refresh token, account number, order history, positions, quotes, contract symbols, bid/ask data, or personal information. Submitted snapshots are calculated in memory and are not stored. Anonymous active-session presence stores only a one-way digest of a random browser-session ID for 90 seconds.

## Install

Python 3.10 or newer is recommended.

```bash
git clone https://github.com/adamk/foxchase-gex-community.git
cd foxchase-gex-community
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add your own Schwab developer app values to `.env`, then authorize it:

```bash
python -m gex_client.login
```

The OAuth token is written with user-only permissions to `~/.foxchase-gex/schwab_tokens.json` by default.

Start the local dashboard:

```bash
python run.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The web server binds only to the loopback interface, so it is not exposed to other devices on your network.

## Test

```bash
pip install pytest
pytest
```

## Important

This project is research software, not investment advice. It does not place trades. You are responsible for complying with the terms and market-data rights attached to your brokerage/developer account. Do not commit `.env` or token files; both are ignored by Git.

