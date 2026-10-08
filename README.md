# Foxchase Trading GEX

This is the open-source, self-run client for the Foxchase Trading SPX/NDX 0DTE gamma-exposure dashboard. Your Schwab app credentials and OAuth tokens stay on your computer. The client retrieves the option chain locally, removes every field the calculation does not need, and sends the minimized numeric snapshot to the private Foxchase Trading calculation API.

The GEX calculation and Foxchase Trading Read classifier are intentionally not included in this repository. This client is free to run yourself; a fully hosted version may be offered separately after the required brokerage/data approvals are in place.

Many gamma-exposure dashboards require a recurring subscription. Foxchase Trading GEX provides a free, self-run alternative for technically comfortable traders who are willing to connect and maintain their own approved Schwab developer app.

Subject to the required brokerage and market-data approvals, Foxchase Trading may also offer a premium browser-based version with simplified OAuth, managed hosting, automatic updates, and no local installation.

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

Python 3.9 or newer is required.

```bash
git clone https://github.com/adamk/foxchase-gex.git
cd foxchase-gex
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Connect Schwab in the local dashboard

Start Foxchase GEX:

```bash
python run.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). On first run, the local
dashboard opens the Schwab setup wizard. Enter the app key and app secret from
your Schwab developer app; the redirect URI defaults to `https://127.0.0.1` and
must exactly match the callback registered with Schwab.

Choose **Save & Connect Schwab**. Foxchase GEX saves these credentials on your
computer in `~/.foxchase-gex/schwab_credentials.json` with file mode `0600` in a
user-only `0700` directory, then opens the existing Schwab authorization flow.
The secret is submitted only to this local app and to Schwab's token endpoint;
it is not returned to the browser after saving and is not sent to Foxchase
Trading. Foxchase GEX also creates a local session-signing secret in the same
private directory when needed.

Sign in to Schwab and approve the developer app. Schwab redirects to
`https://127.0.0.1/?code=...`; that local HTTPS address may not load. Copy the
complete URL from the browser address bar, return to the local setup page, and
paste it into the form. The code is exchanged locally through the existing
OAuth implementation and is neither displayed again nor logged. After a
successful exchange, the dashboard shows **Schwab Connected** and the local
Schwab authorization status.

By default, `python run.py` binds to loopback only. Keep it on `127.0.0.1` or
`localhost` while using the setup wizard; the wizard is disabled for
non-loopback hosts. Credentials and OAuth tokens stay on your computer.

## 4. Advanced environment and CLI setup

Manual `.env` setup remains supported for advanced users. Copy the example and
set the values for your own Schwab developer app:

```bash
cp .env.example .env
```

```dotenv
SCHWAB_CLIENT_ID=your_app_key
SCHWAB_CLIENT_SECRET=your_app_secret
SCHWAB_REDIRECT_URI=https://127.0.0.1
```

Environment credentials take precedence over the GUI credential file. To use
the existing terminal flow instead, run `python -m gex_client.login`, open the
printed authorization URL, and paste the full Schwab redirect URL at the hidden
terminal prompt. The token is stored with user-only permissions in
`~/.foxchase-gex/schwab_tokens.json` by default.


## Production UI source and deployment

The production-compatible three-file UI source and guarded deployment mapping
are documented in [`deploy/PRODUCTION_UI.md`](deploy/PRODUCTION_UI.md). The
deployment script requires an exact production UI baseline, creates a
timestamped rollback backup, and never writes root-level `/SHA256SUMS`.

## Local historical archive

Set `FOXCHASE_GEX_DATA_DIR` to the directory where computed snapshots should be
stored. The dashboard automatically shows the historical-session controls once
that directory contains archived sessions. `FOXCHASE_GEX_REQUIRED_MOUNT` can be
set to a mounted drive root; the collector then refuses to write when that drive
is unavailable, preventing data from falling back to the system disk.

Run the dashboard continuously, then start the headless collector:

```bash
export FOXCHASE_GEX_DATA_DIR=/mnt/t7/foxchase-gex/archive
export FOXCHASE_GEX_REQUIRED_MOUNT=/mnt/t7
python -m gex_client.collector
```

The default interval is 60 seconds for SPX and NDX from 09:30 through 16:00 ET
on weekdays. Set `FOXCHASE_GEX_CAPTURE_SECONDS` to a value of 30 or greater to
change it. The archive contains computed GEX results only, not Schwab credentials,
OAuth tokens, account data, or raw option chains.

### Shadow strategy audit

Each successful collector snapshot also appends a shadow-only structural audit
under `FOXCHASE_GEX_DATA_DIR/audit/SPX/YYYY-MM-DD.jsonl`. The record preserves
net gamma, the modeled zero-gamma flip, call/put walls and strength, imbalance,
wall migration, and provisional bull-put, call-credit, and iron-condor
allow/block labels. These labels are research observations only: they never
connect to a broker and cannot place, resize, or close a trade. Raw features are
kept alongside every label so thresholds can be re-scored later without
recollecting the market data.

Set `FOXCHASE_GEX_FORWARD_AUDIT=0` to disable the derived audit while retaining
the ordinary GEX snapshot archive.

The collector includes the production-compatible `foxchase_shadow_gex_v2`
audit module. An optional `FOXCHASE_ALGO_STATE_PATH` supplies same-day research
context; without it, context-dependent observations remain `WATCH`. Optional
dashboard telemetry requires an explicitly configured URL and token. Keep those
values and all state files outside Git. Telemetry is best-effort and is not a
broker/order interface.

The units in `deploy/` are reference examples for a Pi with an external archive
mount. Review the user, paths, mount dependency and bind address before installing;
the dashboard example binds to `0.0.0.0`, unlike the loopback default. They do not
contain the private production identity overrides, credentials or trading units.

## Test

```bash
pip install pytest
pytest
```

## Important

This project is research software, not investment advice. It does not place trades. You are responsible for complying with the terms and market-data rights attached to your brokerage/developer account. Do not commit `.env` or token files; both are ignored by Git.
