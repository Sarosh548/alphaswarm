# AlphaSwarm — Regime Router Bot Kind

Built 2026-11-20. Paper trading only, as always: no real-money code
was added, no exchange keys exist anywhere, no OS schedules/crons
were created. `~/workspace/alpha-paper-forward/` (the frozen v1/v2/
breakout runners) and the production instance's data were not
touched; the only live change is one new bot on the DEMO instance.

## What was built

**1. The classifier — `engine/regime.py`.** For a symbol's closed 1h
candles it labels the current market situation with exactly one of
five regimes, from three causal measures:

| Measure | Definition |
|---|---|
| Kaufman Efficiency Ratio, ER(20) | \|net change over 20 bars\| / total path travelled. 1.0 = straight line, ~0 = churn. |
| Wilder ADX(14) | Trend *strength* (direction-blind), Wilder smoothing, first defined at bar 27. |
| Bollinger Bandwidth percentile | (upper − lower) / mid of the 20-bar 2σ bands, mid-rank percentile vs its own trailing 120 values. |

**Thresholds (fixed in code, documented in the module, NOT tuned
against any result — this is a classifier, not a strategy):**

| Label | Rule | Threshold provenance |
|---|---|---|
| TREND_EXPANSION | ADX ≥ 25 **and** ER ≥ 0.50 **and** bandwidth pct ≥ 0.50 | Wilder's trend level; the project's Phase 2 diagnosis bucket (ER ≥ 0.50 is where v1's entries performed best); above-median bandwidth = volatility expanding |
| CLEAN_TREND | ADX ≥ 25 **and** ER ≥ 0.30 (not expansion) | Wilder + an efficiency floor, so a direction flip inside the window (high ADX, ~0 ER) cannot pass as a trend |
| COMPRESSION | bandwidth pct ≤ 0.20 (lowest quintile), not a trend above | The TTM-Squeeze style relative definition: lowest bandwidth in its recent history |
| RANGING | ER ≤ 0.25 **and** ADX < 20 | The project's own mean-reversion research gate (ER ≤ 0.25); Wilder's "no trend" zone |
| MIXED_UNCLEAR | everything else, incl. insufficient history | Fail-safe: unknown ground means sit out |

Precedence is top-down: trend before compression (a trending
market's bandwidth is rarely in its lowest quintile, and the trend
is the tradeable fact when they conflict), compression before
ranging (the coil is the more specific signature; both sit out
today anyway). Insufficient history (< 50 defined bandwidth values,
or ADX/ER still warming up) yields MIXED_UNCLEAR.

**2. The router — `engine/engine_router.py`, bot kind `"router"`.**
Each cycle, per symbol in the bot's universe (fixed list or
screener top-N, fail-closed exactly like the other kinds):

| Regime | Routed to | Status of that specialist |
|---|---|---|
| TREND_EXPANSION | Breakout signal source (`engine/breakout.py`, verbatim) | The project's only strict-bar passer (+12.60% after fees, both windows, shared account) |
| CLEAN_TREND | v2-gated analyst consensus (4 agents → `decision.build_proposal` → `strategy_v2.filter_proposal`) | Present in the platform; the router reuses it unchanged |
| COMPRESSION | **no trade** | Squeeze candidate was a near-miss that failed the bar — no proven specialist |
| RANGING | **no trade** | Mean-reversion candidate failed outright — the seat is empty |
| MIXED_UNCLEAR | **no trade** | v1's losses clustered exactly here; sitting out is the proven-correct behaviour |

**Journaling standard:** every symbol, every cycle gets a `cycles`
row carrying the regime label, the three measures, and the routing
outcome — which specialist was consulted and what it decided, or the
plain-language sit-out reason. A routed trade's stored reason starts
with the routing explanation ("ROUTED BY THE REGIME ROUTER: …
classified the market situation as CLEAN TREND — efficiency ratio
0.32, trend strength ADX 35.3 … handed the entry decision to the
v2-gated analyst consensus…") followed by the specialist's own
plain-language reason, unchanged.

**Execution is the platform's, unchanged:** the deterministic risk
policy (≤ configured risk %/trade, max 2 open, daily-loss halt,
stale-data refusal, kill switches), 0.1%-per-side paper fees, TP1
partial booking at 1R, break-even stop move, trailing stop (via
`execution.watch_exits`, which runs before classification each
cycle). Per-bot scheduler fault isolation applies as to every kind.
The agent-based "test position" button is refused for router bots,
like breakout bots: a router may only trade what its routing rules
select. Wiring: `botrunner.run_cycle` dispatches kind `"router"`;
the create-bot API accepts it; the admin create form offers it;
bot detail / live trades / comparison / kill switches work
unchanged.

## Tests

Full acceptance suite: **123/123 checks passed** (was 91/91 before
this build and the parallel range-specialist build; +16 checks for
the router, the rest are the range kind's). New coverage (Part 1e):

- *Classifier (7):* accelerating trend → TREND_EXPANSION (ER 1.000,
  ADX 100, bbw_pct 0.996); steady decelerating trend → CLEAN_TREND;
  square-wave sideways → RANGING (ER 0.000, ADX 18.3); decaying
  oscillation → COMPRESSION (bbw_pct 0.004); a rise fully retraced
  inside the window → MIXED_UNCLEAR (ER 0.003 despite ADX 25.4 —
  the ER-agreement rule doing its job); ER of a straight line = 1
  and of a square wave = 0; 30 bars of history fails safe to
  MIXED_UNCLEAR.
- *Router cycles, mocked (6):* RANGING + MIXED_UNCLEAR universe
  journals one regime row per symbol and opens nothing; a
  TREND_EXPANSION series with a fresh Donchian cross (wicks hold
  the closes inside the channel until a final thrust bar) opens
  exactly one trade through the breakout specialist, with the
  routing explanation in its stored reason and the measures in
  the journal; re-running the same signal bar opens no duplicate.
- *HTTP (3):* admin creates a router bot; a live cycle runs and
  its summary reports the regime tally (in one run: "3 symbols
  checked, 1 paper trade(s) opened, 1 sat out by regime. Regimes:
  CLEAN_TREND 1, COMPRESSION 1, MIXED_UNCLEAR 1" — the opened trade
  came through the CLEAN_TREND consensus route); test positions
  are refused (400).

## Demo bot (demo instance only, port 8001)

Bot #8 **"Regime Router — Situation Specialists"** — kind `router`,
screener universe 20, 60-minute cycle, status **running**, equity
$10,000, 0 trades so far.

First-cycle verification, stated honestly: at build time this
network cannot complete the screener's ~18 MB exchangeInfo fetch
(the connection is cut at ~12.4 MB on every attempt, and the
fallback host is geo-blocked, HTTP 451) — the same failure the
demo's other screener bots (#4, #6) are currently failing closed
on. Bot #8's first five cycle attempts therefore journaled
`universe_failed` and opened nothing, which is the designed
fail-closed behaviour. To verify regime journaling end-to-end on
the real instance, ONE cycle was then run with the universe
temporarily set to fixed (BTC/ETH/SOL), after which the bot was
restored to screener/20 (its current, running configuration; its
hourly cycles will classify the screener universe as soon as the
data host completes a fetch, exactly as #4/#6 will). That verified
cycle journaled:

| Symbol | Regime (measures) | Outcome |
|---|---|---|
| BTCUSDT | MIXED_UNCLEAR (ER 0.180, ADX 37.1, bbw_pct 0.771) | Sat out — strong but inefficient move; the ER rule refused to call it a trend |
| ETHUSDT | CLEAN_TREND (ER 0.324, ADX 35.3, bbw_pct 0.871) | Routed to v2-gated consensus; agents split 2 short / 2 none → no consensus proposal → no trade, votes journaled |
| SOLUSDT | COMPRESSION (ER 0.068, ADX 28.4, bbw_pct 0.013) | Sat out — no proven compression specialist |

Data sources that cycle were mixed live/sample (the journal rows
say which), because candle fetches were also intermittently
failing at that moment — the platform's sample fallback kept the
cycle honest and labelled rather than dead.

## Incidental robustness fixes (separate from the router)

Today's degraded network exposed a latent bug class: several fetch
paths did not catch `http.client.IncompleteRead` (a connection cut
mid-body), so instead of their documented fallback they crashed the
cycle. Fixed by adding `http.client.HTTPException` to the catch
tuples in `engine/data.py` (candle fetch → sample fallback),
`engine/screener.py` (`fetch_universe` → next host / RuntimeError),
and `engine/events.py` (`get_fear_greed` → cached value). Also,
`botrunner._run_event_cycle`'s screener-failure fallback called
`screener.load_universe()`, which itself raises when no cached
universe exists yet (fresh data dir) and hosts are unreachable —
the fallback now degrades to an empty symbol dictionary and the
news poll continues, instead of killing the Event Watcher cycle.
These change nothing under a healthy network; under today's they
are the difference between "Cycle failed" and an honest degraded
cycle. (The frozen forward runners have their own copies and were
not touched.)

## Limitations / notes

- **Routing quality is unproven by construction.** The classifier's
  thresholds are principled and documented, but whether routing
  beats running the specialists directly can only be settled by
  this bot's forward paper record, which starts from zero today.
  Nothing about the router should be described as profitable.
- A label is a routing hint, not an order: each specialist still
  applies its own entry rules and the risk policy still gates
  every trade (the demo cycle above shows the consensus route
  declining to trade inside a correctly-labelled trend).
- ADX is direction-blind and rises on any sustained one-way move,
  including one leg of a smooth oscillation; the ER-agreement
  requirement on both trend labels is the guard, and a slow smooth
  sine can still read as trend-like to both. The specialists' own
  gates are the second line of defence.
- Three of five regimes currently sit out, by design: the router's
  catalogue grows only when a specialist passes the project's
  strict bar (the range seat is the known gap).
- This build ran concurrently with the range-specialist build
  (bot kind `"range"`, demo bot #7); both dispatches coexist in
  `botrunner`/`app.py` and the combined suite is the 123/123
  reported above.
