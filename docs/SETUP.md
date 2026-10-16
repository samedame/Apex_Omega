# Setup

## 1. Discord webhook
Server Settings -> Integrations -> Webhooks -> New Webhook -> choose the channel -> Copy Webhook URL.
Test it: `DISCORD_WEBHOOK_URL=... python -m apex_omega test-webhook`.

## 2. Run locally (any always-on machine; recommended)
Needs **Python 3.10 or newer** (macOS's built-in `python3` is 3.9: install a newer one with `brew install python@3.12` or from python.org, then use `python3.12` below).
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'   # omit for a dry run that prints alerts
export ACCOUNT_EQUITY=1500                                          # optional: suggested sizing + paper account
python -m apex_omega seed-history          # one-off: loads the last 20+ sessions of RTH closes/ranges
python -m apex_omega run                   # start before 09:30 ET; stops itself at 16:02 ET
python -m apex_omega eod                   # after 16:20 ET: reconcile, ledger, EOD post
```
Cron/launchd example (machine on ET or UTC, adjust): `20 9 * * 1-5  cd /path/apex-omega-bot && python -m apex_omega run`
and `30 16 * * 1-5  ... python -m apex_omega eod`. `python -m apex_omega status` shows what the bot sees.

## 3. GitHub Actions (optional)
Push the repo, add secret `DISCORD_WEBHOOK_URL` (and optionally variables `ACCOUNT_EQUITY`, `PROVIDER`; see section 4 for the Tradovate secrets). Workflows: `tests`, `omega-session`
(starts 09:20 ET via two UTC crons + a DST guard, hands off to a fresh job every 5.5 h), `omega-eod`. GitHub cron can start late; for punctual starts
use an external scheduler to call `workflow_dispatch`. A 6.5 h session every trading day is ~8,000 Actions minutes a month: free on a public repo
(which exposes your ledger), but far above the 2,000 free minutes of a private repo. These workflows were validated for syntax only, not run on GitHub.

## 4. Real-time data
Default `PROVIDER=yahoo` is ~10 min delayed (alerts are labelled DELAYED). Two live options:

**Tradovate (`PROVIDER=tradovate`)**: real-time CME NQ 1-minute bars over Tradovate's market-data WebSocket.
Requirements on the Tradovate side: a funded LIVE account (> $1,000 equity), the *API Access* add-on, an API key (gives you `cid` and `sec`), and a CME market-data subscription.
Set these as GitHub *secrets* (or env vars locally) and the repo *variable* `PROVIDER=tradovate`:

| Name | Notes |
|---|---|
| `TRADOVATE_NAME`, `TRADOVATE_PASSWORD`, `TRADOVATE_CID`, `TRADOVATE_SEC` | needed for unattended daily runs: the bot logs in itself and renews the token every ~75 min |
| `TRADOVATE_DEVICE_ID` | any stable string (<= 64 chars); keep it the same between runs |
| `TRADOVATE_ACCESS_TOKEN` | optional, manual tests only: a bare token expires ~90 min after Tradovate issued it, so it cannot start tomorrow's session |
| `TRADOVATE_ENV` (variable) | `live` (default) or `demo` |
| `TRADOVATE_SYMBOL` (variable) | optional; default is the auto front month (`NQZ6` ...), rolled 8 days before the 3rd-Friday expiry. `PROVIDER=tradovate:MNQ` uses the micro product |

Check it before relying on it (compare the printed bars with your chart; timestamps are the bar START):
`python -m apex_omega feed-check --provider tradovate`. Lag should be a few seconds. If the feed stalls the existing DELAYED labelling takes over.
Notes: one market-data connection per Tradovate session (the bot holds exactly one); the data licence is personal use only, so keep the repo private and do not republish raw bars.
Roll weeks: the prior-session history (SMA20/ATR) is seeded from Yahoo's continuous contract and may sit a calendar-spread away from the Tradovate contract for a few days around a roll.

**CSV bridge (`PROVIDER=csv:/path/bars.csv`)**: write 1-minute bars (UTC timestamp, open, high, low, close, volume) from any platform. The CSV must include the forming bar (its open is used to price entries).

## 5. What the bot never does
It never places or modifies orders. Tradovate credentials, if you add them, are used for read-only market data only (the code calls no order endpoint). Every alert is a model instruction for you to act on or ignore.
