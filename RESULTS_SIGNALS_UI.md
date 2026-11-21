# RESULTS — Signal-first rebuild (Tradebise-style home) — 2026-11-21

**What this was:** Sarosh tested the live site and found it confusing —
a new user could not tell what to do, where trades happen, or whether
anything is running. His reference: tradebise.com (search a market →
instant BUY/SELL/WAIT with entry, take-profit, stop-loss and the
reason). This build gives AlphaSwarm that front experience on top of
the EXISTING engines. Strategy/risk logic was not touched (see
"Protected files" below). Local build + tests only — NOT deployed.

## What was built

### 1. New module: `engine/signals.py` (read-only)

Computes one market's current signal using exactly the analysis path
the Regime Router bot already uses (`engine_router`'s routing, minus
the trading side effects — nothing here opens trades, writes journals
or touches the risk policy):

1. `regime.classify_regime` labels the market situation.
2. **TREND_EXPANSION** → the Breakout specialist
   (`breakout.breakout_signal`: Donchian-20 fresh cross, fail-closed
   4h trend agreement, volatility guard). A fired breakout maps to
   BUY/SELL with its entry/SL/TP. `confidence` is **null on purpose** —
   a breakout is a rule event, not a vote; inventing a number would be
   dishonest. The reasons say so.
3. **CLEAN_TREND** → the v2-gated analyst consensus
   (`agents.run_agents` → `decision.build_proposal` →
   `strategy_v2.filter_proposal`). No consensus (needs ≥3 of 4 agents,
   avg confidence ≥0.60) → WAIT with the vote tally. v2 filter block →
   WAIT with the block translated to plain words. Passed → BUY/SELL
   with the real `avg_confidence` and per-agent vote lines as reasons.
4. **COMPRESSION / RANGING / MIXED_UNCLEAR** → WAIT: no specialist has
   a proven record in those situations, so the platform sits out and
   says why.

**Fail closed, always.** The data layer silently falls back to bundled
sample data when live data can't be fetched. A signal computed on
sample data would be fabricated, so any non-`"live"` source on either
timeframe returns an honest WAIT card with `source: "unavailable"` and
an error explanation — never numbers. Verified live with a bogus
symbol (`ZZZNOTREAL` → WAIT / unavailable).

**Caching (1 GB server):** single market 120 s, overview 180 s,
in-memory with a lock; only successful live results are cached. The
overview reuses `screener.run_screener(top_n=10, write=False)` (never
writes screener files from this path) +
`screener.build_trading_universe` (tokenized stocks + gold tokens
excluded), capped at 10 cards.

### 2. New endpoints in `app.py` (login required, like all read APIs)

- `GET /api/signal?symbol=BTCUSDT` — accepts `BTC`, `BTC/USDT`, etc.
  (normalised; missing/invalid → HTTP 400). Response shape:
  `{symbol, action: BUY|SELL|WAIT, entry, take_profit, stop_loss,
  confidence, regime, regime_plain, strategy, reasons[], price, as_of,
  source, reasons_visible, disclaimer}`. WAIT ⇒ entry/tp/sl are null.
- `GET /api/signals` — `{signals: [same cards, ≤10], error,
  as_of, universe_note, disclaimer}`. On screener failure: empty list
  + honest `error`, never stale/invented cards.
- **Plan gating follows the platform's own published promise** ("Live
  signals with the agents' reasons" is a listed Pro feature): the
  signal itself — action + levels — is visible to every logged-in
  user; the agents' full reasons are shown to admin / Pro / Premium,
  and Free users get one line saying so (`reasons_visible: false`).

### 3. New default HOME in `static/index.html`

- Headline card: "What is the market doing right now?" + the paper-
  trading / not-financial-advice / no-guarantee note, a market search
  box, and one clear signal card: big action badge (BUY green / SELL
  red / WAIT grey), entry / take-profit / stop-loss / confidence
  tiles, regime + strategy pills, plain-language reasons.
- "Live signals — top markets right now": grid of compact cards from
  `/api/signals`, each with a "See full signal & reasons" button that
  loads it into the search card. Refetch-guarded (60 s client-side,
  plus the server cache).
- "Paper-trading bots": simple cards (status pill, plain kind name,
  paper balance, PnL, win rate, open/closed counts) with ONE obvious
  button — **Start paper trading / Pause paper trading** — reusing
  the existing `POST /api/bots/<id>/start|pause` (admin-only in the
  API; other users see status + "Full record"). The compact stats
  strip (total PnL, win rate, open trades, Fear & Greed) is kept.
- **Advanced nav:** Bot detail, Live trades, News feed, Bot
  comparison, Settings, Billing & plans and Admin now sit behind an
  "Advanced ▾" toggle in the nav; "Full record" on a bot card opens
  Advanced automatically. All existing screens are unchanged and fully
  functional. Terms/Privacy footer links, TEST MODE billing copy and
  every paper-only label are untouched. No profit claims in new copy.

## Real example (live Binance data, this VM, 2026-11-21)

`GET /api/signal?symbol=BTCUSDT` (admin session):

```json
{
  "symbol": "BTCUSDT",
  "action": "WAIT",
  "entry": null, "take_profit": null, "stop_loss": null,
  "confidence": null,
  "regime": "CLEAN_TREND",
  "regime_plain": "an established trend is in place - price is moving in one direction with real strength behind it",
  "strategy": "Analyst consensus (4 agents, v2 safety filter)",
  "reasons": [
    "Market situation: CLEAN TREND - ... (efficiency ratio 0.357, trend strength ADX 53.3, volatility percentile 0.988).",
    "The analyst agents did not reach a tradeable consensus (votes: none 2, short 2). A trade needs at least 3 of the 4 agents agreeing on one direction with an average confidence of at least 60% - so the answer is WAIT.",
    "TrendAgent: short (confidence 100%) - price=58066.71 EMA20=58843.15 ... HTF(4h)=down",
    "MomentumAgent: short (confidence 66%) - RSI14=41.2 MACD_hist=-139.1411",
    "MeanReversionAgent: none (confidence 20%) - ...",
    "VolatilityAgent: none (confidence 30%) - ..."
  ],
  "price": 58066.71, "as_of": 1795240800000, "source": "live",
  "reasons_visible": true,
  "disclaimer": "Paper trading only: ..."
}
```

`GET /api/signals` returned 10 live cards (BTC, ETH, ZEC, SOL, ENSO,
NEAR, XRP, BNB, HYPE, DOGE — from 80 eligible coins; tokenized stocks
and gold excluded). At verification time every one was an honest WAIT
(the market was in clean downtrends with split agent votes, or mixed)
— that is the engines' real output, not a placeholder. The BUY/SELL
paths are proven by the mocked unit tests below (breakout BUY with
entry 340.408 / TP 346.408 / SL 337.408 on the standard expansion
series; consensus BUY carrying the real 0.72 confidence).

Fail-closed proof: `GET /api/signal?symbol=ZZZNOTREAL` →
`action: WAIT, source: "unavailable", error: "Live data is
unavailable for this market right now (the symbol may not exist, or
the data service could not be reached)."`

Homepage: HTTP 200, signal-first sections present, Terms of Service +
Privacy Policy links intact.

## Tests

- Baseline before this build: **123/123**.
- After: **139/139 checks passed** (`python3 tests/run_acceptance.py`,
  exit 0). 16 new checks:
  - 11 mocked unit checks (new Part 1g): ranging → WAIT/sit-out;
    sample-source refused (fail closed); trend-expansion breakout →
    BUY with ordered SL < entry < TP and null confidence; clean-trend
    consensus → BUY with confidence passthrough; v2-filter block →
    WAIT with the plain reason; cache hit does not re-fetch; overview
    capped at 10 with non-crypto excluded; overview screener failure
    fails closed.
  - 5 HTTP checks: `/api/signal` requires login (401); complete card
    shape over live data; missing symbol → 400; `/api/signals` capped
    cards or honest error; served home page carries the signal-first
    markers + honesty copy.

## Protected files — verified unchanged

md5sums recorded before the build and re-checked after; all identical:
`engine/agents.py`, `engine/decision.py`, `engine/policy.py`,
`engine/engine_router.py`, `engine/regime.py`, `engine/range_fade.py`,
`engine/engine_range.py`. No other engine file was modified either;
the forward-runner copies elsewhere on disk were not touched.
`data/` and `data-demo/` were not touched (verification ran on a
scratch dir under /tmp, since removed).

## Files changed

| File | Change |
|---|---|
| `engine/signals.py` | NEW — read-only signal computation + caching |
| `app.py` | +import, +2 routes (`/api/signal`, `/api/signals`), +2 handlers and the plan-gating view helper; `parse_qs` import |
| `static/index.html` | Signal-first home, Advanced nav toggle, signal/bot card rendering + CSS; all other screens unchanged |
| `tests/run_acceptance.py` | +Part 1g unit tests, +5 HTTP checks, registered in `main()` |
| `RESULTS_SIGNALS_UI.md` | This file |

## Notes / limits

- Signal computation reuses live Binance fetches per symbol (2 calls:
  1h + 4h). With the caches, a cold `/api/signals` took ~35 s on this
  VM for 10 markets; warm calls are instant. On the 1 GB production
  micro the first overview load after a restart will feel slow —
  acceptable for v1; a background warmer is a possible follow-up.
- The endpoints require login, consistent with the rest of the
  platform's read APIs (the public/unauthenticated surface remains
  `/api/public/bots` + `/api/plans`).
- NOT deployed. The production micro still runs the previous build.
