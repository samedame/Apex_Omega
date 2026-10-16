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
Push the repo, add secret `DISCORD_WEBHOOK_URL` (and optionally variables `ACCOUNT_EQUITY`, `PROVIDER`). Workflows: `tests`, `omega-session`
(starts 09:20 ET via two UTC crons + a DST guard, hands off to a fresh job every 5.5 h), `omega-eod`. GitHub cron can start late; for punctual starts
use an external scheduler to call `workflow_dispatch`. A 6.5 h session every trading day is ~8,000 Actions minutes a month: free on a public repo
(which exposes your ledger), but far above the 2,000 free minutes of a private repo. These workflows were validated for syntax only, not run on GitHub.

## 4. Real-time data
Default `PROVIDER=yahoo` is delayed. For a live feed write 1-minute bars (UTC timestamp, open, high, low, close, volume) to a CSV with your charting/broker platform
and run with `PROVIDER=csv:/path/bars.csv`. The CSV must include the forming bar (its open is used to price entries).

## 5. What the bot never does
It never places or modifies orders and holds no broker credentials. Every alert is a model instruction for you to act on or ignore.
