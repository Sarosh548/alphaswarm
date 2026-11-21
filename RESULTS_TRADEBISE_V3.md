# RESULTS — Tradebise end-to-end v3 — 2026-11-21

**What this was:** the owner tested the live site again and made two
demands: (1) everything must look LIVE — the signal card showed the
last closed hourly candle's close labelled "Price now · as of
<candle open time>", which read as stale 11 AM data; (2) the full
Tradebise-style end-to-end product, not just a signal home page.
This build delivers both on the existing engines. Local build +
tests only — NOT deployed.

## 1. Live price overlay (display layer only)

**Design.** Two different numbers, two honest labels:

- The **signal** (BUY/SELL/WAIT + entry/TP/SL) is computed from the
  last COMPLETED hourly candle, exactly as before. That is not a
  bug to fix: it is the tested strategy the paper bots actually
  trade — every backtest, the forward paper runs and the bots' own
  cycles all read closed candles via the same data layer (which
  deliberately drops the still-forming candle). Computing signals
  from a half-formed candle would silently change the strategy into
  something untested. So the decision path in `engine/signals.py`
  (regime → Breakout specialist / v2-gated consensus → WAIT) is
  semantically untouched.
- The **live price** is now a separate overlay:
  `signals.fetch_live_price(symbol)` fetches Binance's public
  `GET /api/v3/ticker/price` across `config.BINANCE_HOSTS` (stdlib
  urllib, ~8 s timeout, returns float or None, never raises).
  `get_signal()` / `get_signals()` apply it at RESPONSE time via
  `_with_live_price()`, which shallow-copies the card and sets
  `live_price` on the copy only. The signal cache is never read for
  a price and never mutated by the overlay — a cached card carries
  `live_price: None`, so nothing stale can leak. Fail-closed cards
  (source != "live") get `live_price: None`: no invented numbers
  next to an honest error.

**UI** (same renderer for search card, overview grid and watchlist):
the prominent price is now `Live: $X`; under it,
`Signal price (last completed hour): $Y · signal updated
<as_of + 1h>` (as_of is the candle's open time, so +1h is when that
hour completed and the signal was set). If the ticker fetch fails,
only the honestly-labelled signal price shows. The words "Price
now" no longer exist anywhere in the SPA.

**Live proof (this VM, real Binance data):** BTCUSDT card —
`live_price 57,999.99` vs a direct Binance ticker reference of
57,990.54 taken seconds apart (0.016% drift), while the signal
price stayed 58,066.71 (last closed hour) — two visibly different,
correctly labelled numbers.

## 2. Watchlist

- Storage: new `watchlist` table in `platform_db.py`
  (`user_id, symbol, added_ms`, PK `(user_id, symbol)`), with
  `watchlist_add / watchlist_remove / watchlist_list` following the
  existing `follows` patterns (INSERT OR IGNORE, so duplicates are
  harmless).
- Endpoints (login required, existing auth/routing style):
  `GET /api/watchlist` → `{symbols, signals}` where signals are the
  user's markets' current cards via `signals.get_signal` (with the
  live overlay and the usual Pro-gating of reasons);
  `POST /api/watchlist {symbol}` (validated by
  `signals.normalize_symbol`, 400 on garbage);
  `POST /api/watchlist/remove {symbol}` (do_POST has no DELETE
  handler, so removal follows the POST-action convention).
- UI: every signal card carries a `☆ Watch` / `★ Watching` toggle;
  a "My watchlist" section sits on the home page below the live
  grid and renders the user's cards with the same renderer, with an
  empty-state line when nothing is watched. Toggling re-renders
  from cached card data — it never burns a signal search.

## 3. Public track record

- `GET /api/record` (login required): the latest **50 CLOSED paper
  trades from PUBLISHED bots only**, newest first by exit time,
  reusing exactly the storage path `api_bot_detail` uses
  (`db.list_bots(published_only=True)` + the per-bot journal reader
  `read_bot_trades`). Fields: bot name, symbol, direction,
  entry_price, exit_price, pnl, exit_reason, exit_ts_ms. **No
  outcome filtering of any kind** — losers are included by
  construction, and a test seeds a published-bot loser and asserts
  it appears (and that an unpublished bot's closed trade does not).
- UI: a "Track record" home section headed *"Every closed paper
  trade from published bots — wins and losses alike. Nothing is
  hidden."*, rendered as a table (bot, market, direction, entry,
  exit, paper PnL coloured by sign, how it ended, close time).
  Honest empty state: *"No closed trades yet. When published bots
  close paper trades, they will appear here — wins and losses
  alike."* (verified live: a fresh instance returns `trades: []`).

## 4. Plan signal quota (Tradebise packaging)

- Free plan: **2 single-market signal searches per UTC day**,
  counted in a new `signal_usage(user_id, day, count)` table via
  `signal_usage_count / signal_usage_bump`. The count happens in
  `api_signal` only, after symbol validation (a malformed symbol
  doesn't burn quota) and before the signal is computed. Admin is
  exempt; Pro/Premium are exempt. The `/api/signals` overview grid
  and `/api/watchlist` do NOT consume quota (tested: both still
  work after a Free user is capped).
- Over the limit: **HTTP 429** with
  `{error, quota: {plan, used, limit}, upgrade: true}`. Successful
  Free searches also carry `quota` so the UI can show "N of 2
  signal searches left today". The UI catches the 429 and shows
  the friendly message plus a **See plans** button that opens
  Billing & plans.
- The existing Pro-gating of the agents' reasons is unchanged.
- PLANS copy updated: Free gains *"2 live signal searches a day"*;
  Pro gains *"Unlimited live signal searches"*. All TEST MODE /
  paper-only honesty copy intact; no profit claims anywhere.

## Tests

- Baseline before this build: **139/139**.
- After: **165/165 checks passed** (`python3 tests/run_acceptance.py`,
  exit 0) — 26 new checks:
  - 10 mocked unit checks (new Part 1h): overlay shows the fresh
    ticker; overlay never mutates the cached card; repeat lookup
    re-fetches the ticker (not the candles); ticker failure →
    `live_price` null with the signal intact; overview re-fetches
    per call with the overview cache unmutated; watchlist
    add/list/remove with duplicates ignored; per-day usage
    counting; plan copy.
  - 16 HTTP checks: watchlist/record/add all 401 unauthenticated;
    add normalises `btc` → `BTCUSDT`; watchlist returns symbols
    with full cards incl. `live_price`; invalid symbol 400; remove
    empties the list; `/api/record` = published-only, newest-first,
    includes the seeded loser, excludes the unpublished bot's
    trade; Free user gets exactly 2 searches (quota used 1, then
    2), the 3rd is a 429 with quota details; grid + watchlist not
    blocked by the quota; Pro exempt; admin exempt (3 searches);
    served home carries the new sections + honest price labels and
    no "Price now".

## Live verification (scratch data dir, port 8199, real Binance data)

Signup → admin; `GET /api/signal?symbol=BTCUSDT` → WAIT /
CLEAN_TREND / source live, live_price 57,999.99 (Binance reference
57,990.54, 0.016% drift) vs signal price 58,066.71; watchlist add
`eth` → ETHUSDT card with live_price 1,633.12 vs signal price
1,640.53; `/api/record` → `[]` on a fresh instance; `/api/plans`
shows the new quota copy; homepage 200 with both new sections.
Scratch dir removed afterwards; `data/` and `data-demo/` untouched.

## Protected files — verified unchanged

`md5sum -c` against the pre-build snapshot: all OK —
`engine/agents.py`, `engine/decision.py`, `engine/policy.py`,
`engine/engine_router.py`, `engine/regime.py`,
`engine/range_fade.py`, `engine/engine_range.py`.
`engine/signals.py` was extended (ticker fetch + overlay) but its
decision path is semantically identical.

## Files changed

| File | Change |
|---|---|
| `engine/signals.py` | +`fetch_live_price`, +`_with_live_price` overlay in `get_signal`/`get_signals`, `live_price` key on cards; decision path untouched |
| `platform_db.py` | +`watchlist` + `signal_usage` tables, +5 methods, PLANS quota copy |
| `app.py` | +`GET /api/watchlist`, `POST /api/watchlist`, `POST /api/watchlist/remove`, `GET /api/record`; Free-plan quota (429) in `api_signal` |
| `static/index.html` | Live/signal price labels, watch toggle on every card, My watchlist + Track record home sections, quota note + 429 → See plans, api() errors carry status/data |
| `tests/run_acceptance.py` | +Part 1h unit tests, +16 HTTP checks, registered in `main()` |
| `RESULTS_TRADEBISE_V3.md` | This file |

## Caveats

- The live overlay adds one small ticker request per card per view
  (10 for a cold overview). Ticker failures degrade to the labelled
  signal price only — never to a made-up number.
- The signal still refreshes on the platform's hourly cadence (plus
  the existing 120 s/180 s caches). That is deliberate: it is the
  tested strategy's timeframe, and the UI now says so in plain
  words instead of dressing a candle close up as "now".
- Quota counts are per UTC day (Tradebise-style daily reset);
  counts persist in the platform DB, so restarts don't reset them.
- NOT deployed. The production micro still runs the previous build.
