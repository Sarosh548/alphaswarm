# AlphaSwarm — Signal-First Multi-Agent Crypto Trading Platform (Paper Trading)

**Live demo:** https://130-210-114-251.sslip.io

Search any market and get a read — **BUY, SELL or WAIT** — with a live
price, entry, take-profit and stop-loss, plus the agents' reasons in
plain language. A team of analyst agents votes on trades, a regime
router matches each market situation to a proven specialist strategy,
a deterministic Risk Manager gates every trade, and every closed paper
trade from published bots appears in a public track record — wins and
losses alike. Nothing is hidden, and no profit is ever promised.

**Paper trading only.** No real-money execution, no exchange API keys
are collected anywhere, and billing is a labelled test mode.

## Run it (one command)

```bash
cd ~/workspace/alphaswarm-platform
python3 app.py
```

Then open **http://127.0.0.1:8000** in a browser.
No dependencies to install — Python 3 standard library only
(`http.server` + `sqlite3`). Optional environment variables:
`ALPHASWARM_PORT` (default 8000), `ALPHASWARM_DATA_DIR`
(default `./data`).

## First user = admin (the owner)

There is no default password. **The first account to sign up becomes
the admin** — that should be the owner. Admins can create/configure/
publish bots, run cycles, use the kill switches, and see the audit log.
Everyone else signs up as a subscriber (Free plan).

## What is inside

- **Bots** (each with its own paper wallet + SQLite journal):
  - *Technical v1* — the original 4-agent consensus strategy
    (Trend / Momentum / Mean Reversion / Volatility) on BTC/ETH/SOL.
  - *Technical v2* — v1 plus the hard 4h-trend filter and the
    Momentum-agreement requirement.
  - *Event Watcher* — non-trading: all-coin screener (700+ USDT pairs
    filtered to a ranked shortlist), news/event polling with the
    CONFIRMED vs UNCONFIRMED confirmation layer, and confirmed alerts.
- **Universe mode** (per technical bot, in Settings): `fixed` = the
  bot's own coin list (default BTC/ETH/SOL); `screener` = every cycle
  re-runs the all-coin screener and analyses its top N coins
  (5–30, default 20; BTC/ETH/SOL always added; tokenized stocks and
  gold excluded from trading). The universe is journaled each cycle.
  The risk policy is unchanged — max 2 open positions per bot however
  wide the universe gets — and a screener failure fails the cycle
  closed (nothing opens). See `RESULTS_ALLCOINS.md`.
- **Execution upgrades** (platform engine only — the frozen forward-run
  copies in `~/workspace/alpha-paper-forward/` do not have these):
  partial profit booking at TP1 (1R, 50% of the position), stop moved
  to break-even, trailing stop (1R behind the best price), and
  realistic paper fees of **0.1% per side** on notional. All stored PnL
  is net of fees.
- **Reporting**: every trade stores a plain-language reason (which
  agents voted what, and the Risk Manager's numbers). Reports show win
  rate, profit factor, max drawdown and Sharpe ratio (daily realised
  returns, annualised √365; shown as "not enough data" until at least
  two days of realised activity exist).
- **Screens**: Home, Bot detail (equity curve, trades + reasons, latest
  agent signals), Live trades (manual close), News feed (rule-based
  Bullish/Bearish/Neutral impact labels, CONFIRMED/UNCONFIRMED badges,
  Fear & Greed), Bot comparison, Settings (coins, risk %, daily loss
  limit, auto-cycle minutes, kill switches), Billing & plans, Admin
  panel (create/publish bots, users, audit log).
- **Kill switches**: per-bot (closes that bot's positions, stops it,
  drops its engine HALT file) and global (closes everything, blocks
  all cycles until an admin clears it). Bots can also auto-run cycles
  on an in-app scheduler (per-bot minutes; no OS cron involved).

## Hard gates (by design — do not remove)

- **PAPER TRADING ONLY.** There is no real-money execution, no
  exchange order placement, and no exchange API-key handling anywhere
  in this codebase. The Premium plan lists auto-trading as
  **NOT YET AVAILABLE**, and there is deliberately **no form anywhere
  that collects exchange keys**.
- **Billing is TEST MODE.** No real payment provider is connected.
  The checkout is a clearly-labelled simulation: any test card number
  (12–19 digits) succeeds, a `test_paid` payment row is recorded, and
  the plan activates. Connecting real Stripe is a later step that
  needs the owner's own Stripe account.
- **No guaranteed-profit language anywhere.** Pricing and performance
  pages carry the disclaimer that paper results are not a promise of
  real returns.

## Data layout

```
data/platform.db          users, sessions, bots, payments, audit log
data/bots/<id>/journal.db that bot's own paper-trading journal
data/bots/<id>/HALT       per-bot kill-switch file (engine input)
data/events.jsonl         point-in-time news/event log (seeded from v3)
data/alerts.jsonl         confirmed-event alerts
data/screener_universe.json / screener_report.md
```

## Tests

```bash
python3 tests/run_acceptance.py
```

39 checks: engine unit tests for the execution upgrades and metrics,
plus a full HTTP acceptance walk (signup/admin bootstrap, bot creation,
live cycles for all three bot kinds, test position with SL/TP + reason,
kill switches, test-mode checkout, plan gating, publishing). The suite
uses an isolated data dir on port 8123 and does not touch the live
instance. Latest result: **39/39 passed** (see RESULTS_PLATFORM.md).

## Relationship to the other folders

`~/workspace/alpha-paper-bot/`, `~/workspace/alpha-paper-v3/` and
`~/workspace/alpha-paper-forward/` are untouched by this platform; the
engine code here is a *copy*, adapted for multi-bot use. The v1/v2
forward paper comparison keeps running from its own frozen copies.
