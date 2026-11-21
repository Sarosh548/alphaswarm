# AlphaSwarm — All-Coins Universe Mode: Build Record

Built 2026-11-19 at the owner's request ("pehle isay test karta hai
sirf BTC/ETH/SOL par kyun — saare coins par work karna chahiye").
Paper trading only, as everywhere in this project. The frozen
forward-run copies in `~/workspace/alpha-paper-forward/` were NOT
touched — their BTC/ETH/SOL universe is part of that comparison's
honesty.

## What was built

**Per-bot universe mode** (new bot settings, stored in the platform DB;
existing databases migrate automatically, existing bots default to
`fixed` and behave exactly as before):

- `universe_mode = "fixed"` — the bot analyses its configured symbol
  list (the old behaviour; default BTC/ETH/SOL).
- `universe_mode = "screener"` — every cycle first re-runs the all-coin
  screener (Binance public data; TRADING USDT spot pairs; fiat/stable
  bases and leveraged tokens removed; 24h quote volume ≥ $5M;
  |24h move| ≤ 60% sanity bound; published score = 0.50 volume +
  0.25 volatility + 0.25 momentum), then analyses the **top N**
  (`universe_size`, default 20, configurable 5–30). BTC/ETH/SOL are
  always added even if they rank below the cut, so the analysis cap
  is N + 3 (max 23 symbols at the default size).
- The chosen universe is **journaled every cycle** as a `UNIVERSE`
  row in the bot's own journal (mode, size, eligible count, excluded
  non-crypto pairs, full symbol list), so any trade can be traced to
  the exact universe that produced it.

**Non-crypto exclusions from the TRADING universe** (they remain in
the screener's own report and in the Event Watcher's world — they are
real market data, just not crypto): tokenized gold (XAUT, PAXG) and
tokenized stocks ("bStocks": base = US stock ticker + trailing "B",
e.g. NVDAB). The rule is an explicit ticker list + suffix rule in
`engine/screener.py`, so real coins that merely end in "B" are NOT
excluded (ARB, BNB, YB all stay tradeable — unit-tested).

**Risk policy unchanged and global per bot.** Same gate as fixed
mode: ≤ configured risk % per trade (default 1%), **max 2 open
positions across the whole universe**, −3% daily-loss halt (or
configured), mandatory SL/TP, stale-data refusal. A wider universe
only means more candidates competing for the same 2 slots — never
more simultaneous risk. Proven by test: 10 symbols all proposing at
once → exactly 2 opened, 8 rejected by rule P5, each opened trade
risking exactly 1% of equity.

**Fail-closed screener.** If the screener fetch fails, the cycle logs
a `universe_failed` journal row and opens nothing — there is no
fallback to a stale universe (the Event Watcher's softer behaviour
was deliberately NOT copied here; trading on stale selection data is
worse than not trading).

**UI/API.** Universe mode + size are exposed in the bot-settings
endpoint (validated: mode ∈ {fixed, screener}, size ∈ 5–30, invalid
values get HTTP 400), in the Settings screen, and in the Admin
create-bot form. Bot summaries carry both fields.

## Live proof (demo instance only, port 8001)

New bot **#4 "Technical v1 — All Coins"** (kind v1, universe mode
screener, size 20) was created under the demo admin and ran **one**
live cycle (~64 s: 1 screener fetch + 20 symbols × 2 timeframes of
live candles). The existing demo bots and their trades were
snapshotted before and after: unchanged (Technical v1's open LONG
BTC test position @ 63,558.01 intact; all statuses/fees identical).

Universe the cycle actually analysed (59 eligible coins after the
screener filters; journal `UNIVERSE` row):

BTCUSDT, ETHUSDT, YBUSDT, MMTUSDT, SOLUSDT, ZECUSDT, XRPUSDT,
NEARUSDT, LUNCUSDT, BNBUSDT, SENTUSDT, PEPEUSDT, HYPEUSDT,
CRCLBUSDT, MUBUSDT, UNIUSDT, DOGEUSDT, TRXUSDT, SNXXBUSDT, SUIUSDT

Excluded as non-crypto in that same run: NVDABUSDT, SOXLBUSDT,
XAUTUSDT, SPCXBUSDT, MSTRBUSDT, GOOGLBUSDT, QQQBUSDT.

Votes (80 agent votes; 26 short / 18 long / 36 none): 19 of 20 coins
produced no consensus proposal. **One paper trade opened: SHORT
UNIUSDT @ 4.946, SL 5.0557, TP 4.7267, risk $100.00 (1% of the
$10,000 paper balance), entry fee $4.51** — Trend/MeanReversion/
Volatility agents short, Momentum long, consensus gate passed on the
short side. Bot equity after the cycle: $10,000.00 (no realised PnL
yet), 1 open position.

### A flaw the live run caught (fixed, disclosed)

That first cycle's universe still contained three bStock pairs —
CRCLBUSDT (Circle), MUBUSDT (Micron), SNXXBUSDT — because their
tickers were not in the initial exclusion list. No trade was opened
on any of them (all three: no proposal), and the demo bot's journal
was NOT rewritten — the record stands as it happened. The ticker
list was then extended from a full live scan of every eligible base
ending in "B" (adding CRCL, MU, SNXX, SOXS, SKHY, INTC, KORU, SNDK,
TQQQ). A read-only re-check with the extended list excludes all 16
non-crypto pairs found live (NVDAB, SOXLB, XAUT, CRCLB, MUB, SPCXB,
SNXXB, INTCB, KORUB, SKHYB, SNDKB, MSTRB, SOXSB, GOOGLB, TQQQB,
QQQB) and the trading universe comes out clean. PAXG (tokenized
gold) was added to the gold exclusion at the same time. Binance's
public exchangeInfo exposes no machine-readable "this is a stock"
flag (checked), so a maintained ticker list is the honest mechanism
— and it will lag future bStock listings until updated.

## Tests

Full acceptance suite: **57/57 passed** (was 39/39 before this
change; +18 checks). New coverage:

- Screener-mode universe on mocked data = ranked top N + majors,
  with SOL included despite ranking below the cut; analysis cap
  respected; bStock/gold exclusions incl. all tickers observed live;
  real B-ending coins (ARB, BNB, YB) kept.
- Screener fetch failure raises at the builder level, and at the
  botrunner level the cycle **fails closed**: `universe_failed`
  journaled, nothing opened.
- Wide-universe risk caps: 10 simultaneous proposals → exactly 2
  open, rest rejected by P5, per-trade risk ≤ 1%.
- Screener-mode cycle journals its universe; settings endpoint
  round-trips universe mode/size and rejects size 99 / size 3 with
  HTTP 400; create-bot accepts and validates the new fields.
- All pre-existing checks (execution upgrades, metrics, auth,
  plan gating, kill switches, checkout) still pass.

## Limitations — read these plainly

1. **No honest backtest of a screener universe is possible with the
   data we have.** The screener is point-in-time: today's top 20 is
   not the top 20 of three months ago, and reconstructing past
   universes from today's listings bakes in survivorship bias (the
   coins that died are invisible). So the wide universe can only be
   judged on **forward paper results** — cycles run from now on,
   each with its universe journaled. Any "all-coins backtest" number
   anyone shows you from current data is flattered by construction.
2. **More coins = more noise and more load, not automatically more
   profit.** The screener's score crowns whatever is hot (in the
   live run: YB, MMT, LUNC in the top ranks) — hot coins are where
   both the pumps and the dumps live. Each screener cycle also costs
   a full-market fetch plus up to 23 symbols × 2 timeframes of
   candles (~64 s for the live proof cycle), and cycles are
   serialised across bots on this platform.
3. The consensus gate fired on only 1 of 20 coins in the live cycle
   — a wide universe mostly produces *more rejections*, which is the
   risk system working as designed, but it means trade frequency
   will not scale with universe size.
4. The bStock exclusion list is maintained by hand (see the flaw
   above); a newly listed tokenized stock can slip into the trading
   universe until the list is extended. It can only ever be
   *analysed* — it still has to pass the same consensus + risk gate
   to be traded — but the gap is real.
5. One live cycle proves the plumbing, not an edge. The standing
   verdict is unchanged: neither v1 nor v2 has a demonstrated robust
   edge, and widening the universe does not change that verdict — it
   only gives the strategies more candidates to be wrong (or right)
   about, under the same risk caps.
