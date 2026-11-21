# AlphaSwarm Platform — Build Results

Built 2026-11-19 in `~/workspace/alphaswarm-platform/`, following the
owner's blueprint (`AlphaSwarm_Multi-Agent_Crypto_Trading_Platform.docx`)
around copied AlphaPaper engine code. The source folders
(`alpha-paper-bot/`, `alpha-paper-v3/`, `alpha-paper-forward/`) were
only read, never modified. No schedules/crons were created; nothing was
deployed beyond a local server started for verification.

## Stack used

- Python 3.12 standard library only: `http.server.ThreadingHTTPServer`,
  `sqlite3`, `hashlib` PBKDF2. (Flask/FastAPI are not installed in this
  environment; the task allowed stdlib and it proved fully sufficient.)
- Single-file SPA frontend (vanilla JS + inline SVG equity curve).
- Engine: copied AlphaPaper v1/v2 + v3 screener/events code, adapted so
  each bot runs with its own patched config under a global engine lock,
  with its own journal DB per bot.

## Server status (as observed at delivery)

- Running in the background: `python3 app.py`, PID 24924, log at
  `server.log`.
- URL: **http://127.0.0.1:8000**
- Verified with curl: `GET /` → 200; `GET /api/home` unauthenticated →
  401; `GET /api/plans` → 200 with `test_mode: true`.
- The production instance starts empty: no users or bots yet. The
  first signup becomes admin (see Admin bootstrap).

## Admin bootstrap

First registered user automatically gets `role=admin` (implemented in
`platform_db.create_user`: role is admin iff the users table is empty).
Passwords: salted PBKDF2-HMAC-SHA256, 200,000 rounds; sessions are
random 32-byte tokens in an HttpOnly cookie, 7-day expiry.

## Screens delivered (all 7 + admin)

1. **Home** — total paper PnL, win rate, open trades, Fear & Greed,
   bot table, published-bots public page with Follow buttons.
2. **Bot detail** — equity curve (SVG, from the journal), metrics
   (win rate, profit factor, max drawdown, Sharpe, fees), latest
   per-symbol agent votes, trade list with plain-language entry
   reasons and exit details, admin actions (cycle/start/pause/stop/
   publish/kill/test position).
3. **Live trades** — all open positions across visible bots with
   entry/SL/TP1/TP, last closed hourly price, estimated unrealised
   PnL, and an admin manual-close button.
4. **News feed** — seeded with the 115 real events already collected
   by the v3 branch (plus live polls when the Event Watcher runs);
   headline, source, category, CONFIRMED/UNCONFIRMED badge,
   rule-based Bullish/Bearish/Neutral impact label (labelled as
   rule-based in the UI), confirmed alerts list, Fear & Greed.
   Plan-gated: Pro/Premium or admin.
5. **Bot comparison** — all visible bots side by side with the same
   metrics.
6. **Settings** — coins, risk %, daily loss limit, auto-cycle minutes;
   per-bot and global kill switches.
7. **Billing & plans** — Free / Pro $19 / Premium $49 with TEST MODE
   banners; Premium's auto-trading feature is listed as
   "NOT YET AVAILABLE — paper-trading only"; simulated checkout
   activates the plan end to end. Risk disclaimer on the page.
- **Admin panel** — create/configure bots, publish/unpublish, user
  list, audit log.

## Bots & engine behaviour (verified live)

- v1 bot cycle on live Binance public data: "3 symbols checked,
  0 paper trade(s) opened" (agents split — the consensus gate working,
  same behaviour as the standalone engine).
- v2 bot cycle: same, with the v2 filter active.
- Event Watcher cycle (live): "Screener: 59 eligible coins, top 5:
  BTCUSDT, HUMAUSDT, ETHUSDT, SOXLBUSDT, LUNCUSDT. News: 115 new
  event(s) from 3/3 sources. 5 new CONFIRMED alert(s)..." — in the
  throwaway test instance; the production instance's store is the
  seeded copy and will grow when its own watcher bot runs.
- Admin "Open test position" opens a position through the *real*
  policy gate on the live snapshot, labelled in its stored reason as
  a TEST POSITION (not a strategy signal), with the agents' actual
  votes quoted. Observed: LONG BTCUSDT @ 64038.01 (live data).

## Execution upgrades (platform engine copy only)

- Fees: 0.1% per side on notional, charged at entry and every exit.
- Partial booking at TP1 = 1R: 50% closed, stop → break-even.
- Trailing stop after TP1: 1R behind best price.
- Unit-verified end to end: entry 100, SL 90, TP1 110, TP 120, qty 10 →
  partial +49.45 net, stop 100 → trailed to 101 (same candle's high)
  → 105 after a push to 115 → closed at 105 by the trailing stop;
  final PnL +72.92 net of all fees (fees_paid 2.08).

## Acceptance test results

`python3 tests/run_acceptance.py` — **39/39 checks passed**
(15 engine/metrics unit checks + 24 HTTP end-to-end checks on an
isolated instance, port 8123). Covered: admin bootstrap, plan gating
(403 for Free on news), bot creation for all 3 kinds, live cycles,
test position with SL/TP/TP1 + reason, per-bot kill switch closing
positions, settings update, TEST-MODE checkout activating Pro,
CONFIRMED + UNCONFIRMED badges present, published bot + reasons
visible to the subscriber, global kill switch blocking cycles and
clearing, comparison data, admin-only endpoints.
One test expectation was corrected during the run (after TP1 the stop
is 101, not 100, because the same candle's high legitimately trails it
past break-even) — engine behaviour was verified correct, the test was
wrong.

## What is simulated vs real

- **Real**: market data (Binance public API), news/events (Binance
  announcements + CoinDesk + Cointelegraph RSS), Fear & Greed, all
  agent voting, risk gating, metrics, auth, plan gating, publishing.
- **Simulated**: all trading (paper journal only — by design), all
  payments (TEST MODE checkout; no Stripe keys exist or are used).
- **Sample fallback**: if the network is down, the engine trades on
  clearly-labelled built-in sample data, exactly as in AlphaPaper.

## Limitations (honest list)

- Single-process app: bot cycles are serialised under one lock; fine
  for the owner's scale, not a multi-tenant SaaS architecture.
- Blueprint items intentionally NOT built, per the hard gates: real
  live pilot trading, Premium auto-trading via user exchange keys
  (no key collection exists at all), real Stripe billing.
- Blueprint stack items not used: FastAPI/LangGraph/Prophet/Postgres/
  Redis/ChromaDB/React — the stdlib app covers the same behaviour for
  this stage; the Forecast Agent from the blueprint has no equivalent
  (the four proven analyst agents + event agent are what run).
- News sentiment is rule-based (v3 keyword/category classifier), not
  an LLM — labelled as such in the UI.
- Live-trades "current price" is the last *closed* hourly candle
  (cached 60s), so unrealised PnL can lag the spot price by up to an
  hour; it is labelled an estimate.
- Sharpe needs ≥2 days with realised activity; until then the UI
  shows "not enough data" rather than a made-up number. No bot has a
  meaningful paper track record yet — the 4–8 week paper run from the
  blueprint is still ahead, and nothing here changes the standing
  verdict that neither v1 nor v2 has a demonstrated robust edge.
- The server binds to 127.0.0.1 only and has no TLS; it is a local
  verification instance, not a public deployment.
