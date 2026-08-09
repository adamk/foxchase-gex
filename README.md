# Foxchase Trading GEX

This is the open-source, self-run client for the Foxchase Trading SPX/NDX 0DTE gamma-exposure dashboard. Your Schwab app credentials and OAuth tokens stay on your computer. The client retrieves the option chain locally, removes every field the calculation does not need, and sends the minimized numeric snapshot to the private Foxchase Trading calculation API.

The GEX calculation and Foxchase Trading Read classifier are intentionally not included in this repository. This client is free to run yourself; a fully hosted version may be offered separately after the required brokerage/data approvals are in place.

## Data boundary

The EC2 request contains only:

- symbol and underlying spot;
- current 0DTE expiration date;
- option type, strike, open interest, gamma, volatility fallback, and multiplier.

It does **not** contain your Schwab client ID, client secret, access token, refresh token, account number, order history, positions, quotes, contract symbols, bid/ask data, or personal information. Submitted snapshots are calculated in memory and are not stored. Anonymous active-session presence stores only a one-way digest of a random browser-session ID for 90 seconds.

## 1. Create a Schwab developer app

You need a regular Schwab brokerage account and a separate account on the [Schwab Developer Portal](https://developer.schwab.com/).

1. Register or sign in at the Schwab Developer Portal.
2. In your developer profile, request the **Individual Developer** role if you do not already have it.
3. Request access to **Trader API - Individual** and wait for approval.
4. Open the developer dashboard and create an app.
5. Select **Market Data Production**. This dashboard reads option-chain market data and does not place orders, so Accounts and Trading access is not required.
6. Use an app name such as `Foxchase Trading GEX Local`.
7. Set the callback URL to exactly `https://127.0.0.1` with no trailing slash.
8. Submit the app and wait until its status is **Ready for use**. A pending or provisionally approved app will not authenticate.
9. Open the approved app's details and copy its app key/client ID and app secret. Never post either value or commit them to Git.

Schwab may change the portal labels. The callback URL in the portal and `.env` must match exactly, including capitalization, protocol, port, path, and trailing slash.

## 2. Install Foxchase Trading GEX

Python 3.10 or newer is recommended.

```bash
git clone https://github.com/adamk/foxchase-gex.git
cd foxchase-gex
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```dotenv
SCHWAB_CLIENT_ID=your_app_key
SCHWAB_CLIENT_SECRET=your_app_secret
SCHWAB_REDIRECT_URI=https://127.0.0.1
```

## 3. Authorize Schwab

Run:

```bash
python -m gex_client.login
```

The command prints a Schwab authorization link. Open it, sign in with your normal Schwab brokerage credentials, approve access, and select the applicable account. Schwab then redirects the browser to `https://127.0.0.1/?code=...`. It is normal for that local HTTPS page not to load. Immediately copy the **entire URL from the browser address bar** and paste it into the terminal prompt. The pasted input is hidden.

The OAuth token is then written with user-only permissions to `~/.foxchase-gex/schwab_tokens.json` by default. If authorization fails, verify that the callback URL matches exactly and that the app says **Ready for use**.

## 4. Run the dashboard

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
