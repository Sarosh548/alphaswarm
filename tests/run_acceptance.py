#!/usr/bin/env python3
"""AlphaSwarm platform acceptance tests.

Part 1 (unit, in-process): the platform execution upgrades - partial
profit booking at TP1, break-even stop move, trailing stop, paper fees -
and the metrics module (win rate, profit factor, drawdown, Sharpe).

Part 1b (unit, mocked): the all-coins universe mode - screener-picked
trading universe, bStock/gold exclusions, fail-closed screener
behaviour, and risk caps binding under a wide universe.

Part 1c (unit, mocked): the breakout bot kind - Donchian-20 signal
logic, fail-closed 4h trend labels, the volatility guard, and a
mocked engine cycle that opens a trade with the study's SL/TP
geometry and a plain-language reason (plus per-signal-bar dedupe).

Part 1d (unit): platform hardening - daily backup creation +
7-day retention pruning, scheduler fault isolation (one bot's
exception cannot skip the others), and the Terms/Privacy pages.

Part 1e (unit + mocked integration): the Regime Router - classifier
labels on synthetic series (trend expansion, clean trend, ranging,
compression, mixed), fail-safe on short history, and mocked router
cycles proving sit-out regimes open nothing while a TREND_EXPANSION
with a fresh breakout cross opens exactly one routed trade.

Part 1f (unit + mocked integration): the Range Specialist candidate
(range2, forward-proof only) - stochastic/gate logic on synthetic
series (the self-gate refuses every bar of a trend, including a bar
where a stochastic cross fires inside a pullback; the gate opens in
a ranging box and proposals carry range geometry: stop 1.5xATR,
target = the far channel edge), and mocked cycles proving a ranging
series truncated at a turn opens exactly one candidate trade with
the candidate disclaimer in its reason, while a trending series
opens nothing and journals its gate-closed sit-outs.

Part 1h (unit, mocked): Tradebise v3 - the fresh live-price overlay
on cached signal cards (the cache is never mutated; a ticker failure
yields live_price=None and an unharmed signal), watchlist + per-day
signal-usage storage, and the plan copy advertising the quota.

Part 2 (HTTP, end to end): starts a throwaway server on port 8123 with
an isolated data dir and walks the blueprint's acceptance flow:
signup -> first user is admin -> admin creates v1/v2/event bots ->
live cycle runs without crashing -> test position carries SL/TP and a
plain-language reason -> kill switch closes it -> a subscriber pays
through the TEST-MODE checkout and the plan activates -> the published
bot and its trade reasons become visible to the subscriber -> news
feed plan-gating -> global kill switch blocks cycles.

Run:  python3 tests/run_acceptance.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from http.cookiejar import CookieJar

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "engine"))

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


# ===========================================================================
# Part 1 - engine unit tests
# ===========================================================================

def unit_tests():
    import config
    import execution
    from journal import Journal

    tmp = tempfile.mkdtemp(prefix="alphaswarm_unit_")
    j = Journal(os.path.join(tmp, "unit.db"))
    prop = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0,
            "stop_loss": 90.0, "take_profit": 120.0, "avg_confidence": 0.7}
    fee = execution.entry_fee(100.0, 10.0)
    check("entry fee is 0.1% of notional", abs(fee - 1.0) < 1e-9,
          f"fee={fee}")
    tid = j.open_trade(0, prop, qty=10.0, risk_amount=100.0,
                       entry_ts_ms=1_000_000, entry_reason="unit test",
                       tp1_price=110.0, entry_fee=round(fee, 2))
    t = j.get_trade(tid)
    check("trade stored with SL/TP/TP1 and reason",
          t["stop_loss"] == 90.0 and t["take_profit"] == 120.0
          and t["tp1_price"] == 110.0 and t["entry_reason"] == "unit test")

    def candle(ts, hi, lo):
        return {"ts": ts, "open": 100.0, "high": hi, "low": lo,
                "close": (hi + lo) / 2, "volume": 1.0}

    # Candle 1: reaches TP1 (110) -> book 50%, stop to break-even (100).
    closed = execution.watch_exits(j, {"BTCUSDT": [candle(2_000_000, 111, 99)]})
    t = j.get_trade(tid)
    check("TP1 books half the position",
          not closed and abs(t["qty"] - 5.0) < 1e-9, f"qty={t['qty']}")
    check("TP1 partial PnL net of fees = 49.45",
          abs(t["partial_pnl"] - 49.45) < 0.02, f"{t['partial_pnl']}")
    # Break-even move sets the stop to entry (100); the same candle's
    # high (111) then trails it to 111 - 1R = 101 for the next candle.
    check("stop moved to break-even, then trailed, after TP1",
          t["stop_loss"] == 101.0 and t["tp1_done"] == 1,
          f"stop={t['stop_loss']}")

    # Candle 2: pushes to 115 -> trailing stop rises to 105, still open.
    closed = execution.watch_exits(j, {"BTCUSDT": [candle(3_000_000, 115, 104)]})
    t = j.get_trade(tid)
    check("trailing stop follows best price (105)",
          not closed and t["stop_loss"] == 105.0 and t["best_price"] == 115.0,
          f"stop={t['stop_loss']} best={t['best_price']}")

    # Candle 3: dips to 104.5 -> trailing stop closes the rest at 105.
    closed = execution.watch_exits(j, {"BTCUSDT": [candle(4_000_000, 112, 104.5)]})
    t = j.get_trade(tid)
    check("trailing stop closes the trade",
          len(closed) == 1 and t["status"] == "closed"
          and t["exit_reason"] == "trailing_stop",
          f"reason={t['exit_reason']}")
    check("final PnL net of all fees = 72.92 (+/- 0.02)",
          abs(t["pnl"] - 72.925) < 0.02, f"pnl={t['pnl']}")
    check("fees_paid accumulates entry+partial+exit fees",
          abs(t["fees_paid"] - 2.075) < 0.02, f"fees={t['fees_paid']}")
    check("equity reflects net PnL", abs(j.equity() - 10072.925) < 0.02,
          f"equity={j.equity():.2f}")
    j.close()

    # Metrics: fabricated 3-day journal.
    import metrics
    j2 = Journal(os.path.join(tmp, "metrics.db"))
    day = 86_400_000
    base_ts = (int(time.time() * 1000) // day) * day - 3 * day
    for i, pnl in enumerate([100.0, -50.0, 80.0]):
        tid = j2.open_trade(0, prop, qty=1.0, risk_amount=10.0,
                            entry_ts_ms=base_ts + i * day)
        j2.close_trade(tid, base_ts + i * day + 1000, 101.0, "take_profit",
                       pnl)
    j2.close()
    m = metrics.compute(os.path.join(tmp, "metrics.db"), 10_000.0, base_ts)
    check("metrics win rate 2/3", abs(m["win_rate"] - 2 / 3) < 0.01,
          f"{m['win_rate']}")
    check("metrics profit factor 3.6", abs(m["profit_factor"] - 3.6) < 0.01,
          f"{m['profit_factor']}")
    check("metrics max drawdown ~0.5%",
          abs(m["max_drawdown_pct"] - 0.495) < 0.05,
          f"{m['max_drawdown_pct']}")
    check("metrics Sharpe computed from daily returns",
          m["sharpe"] is not None, f"sharpe={m['sharpe']}")
    check("metrics equity 10130", abs(m["equity"] - 10130.0) < 0.01,
          f"{m['equity']}")


# ===========================================================================
# Part 1b - all-coins universe mode (fully mocked, no live network)
# ===========================================================================

def _fake_meta_tickers():
    """Fabricated Binance universe: a bStock and gold rank at the very
    top on raw score, a major (SOL) ranks below the cut, ARB merely
    ends in 'B' but is a real coin, TINY is under the volume floor."""
    rows = [
        ("NVDABUSDT", "NVDAB", 9_000_000_000.0, 5.0),
        ("XAUTUSDT", "XAUT", 8_000_000_000.0, 1.0),
        ("SOXLBUSDT", "SOXLB", 7_000_000_000.0, 4.0),
        ("BTCUSDT", "BTC", 6_000_000_000.0, 2.0),
        ("ETHUSDT", "ETH", 5_000_000_000.0, 2.5),
        ("ARBUSDT", "ARB", 4_000_000_000.0, 3.0),
        ("DOGEUSDT", "DOGE", 3_000_000_000.0, 6.0),
        ("SOLUSDT", "SOL", 100_000_000.0, 0.5),
        ("TINYUSDT", "TINY", 1_000.0, 0.1),
    ]
    meta = {s: {"base": b, "status": "TRADING"} for s, b, _, _ in rows}
    tickers = {s: {"quoteVolume": str(qv), "priceChangePercent": str(ch),
                   "lastPrice": "100.0", "volume": "1000"}
               for s, b, qv, ch in rows}
    return meta, tickers, "mock://host"


def universe_tests():
    import config
    import screener

    orig_fetch = screener.fetch_universe
    screener.fetch_universe = _fake_meta_tickers
    try:
        uni = screener.build_trading_universe(top_n=3)
    finally:
        screener.fetch_universe = orig_fetch
    syms = uni["symbols"]
    check("screener universe = ranked top 3 + majors",
          set(syms) == {"DOGEUSDT", "ARBUSDT", "ETHUSDT", "BTCUSDT",
                        "SOLUSDT"}, ", ".join(syms))
    check("bStock + gold excluded from the TRADING universe",
          not ({"NVDABUSDT", "XAUTUSDT", "SOXLBUSDT"} & set(syms))
          and {"NVDABUSDT", "XAUTUSDT", "SOXLBUSDT"}
          <= set(uni["excluded_non_crypto"]),
          ", ".join(uni["excluded_non_crypto"]))
    check("majors always included even below the cut (SOL)",
          "SOLUSDT" in syms)
    check("analysis cap respected (top_n + 3 majors)", len(syms) <= 6,
          f"{len(syms)} symbols")
    check("exclusion rule: ticker+B and XAUT out, real coins kept",
          screener.is_non_crypto_base("NVDAB")
          and screener.is_non_crypto_base("SPCXB")
          and screener.is_non_crypto_base("XAUT")
          and screener.is_non_crypto_base("PAXG")
          # bStock tickers observed live after the first list shipped
          and screener.is_non_crypto_base("CRCLB")
          and screener.is_non_crypto_base("MUB")
          and screener.is_non_crypto_base("SNXXB")
          and screener.is_non_crypto_base("TQQQB")
          and screener.is_non_crypto_base("SOXSB")
          and not screener.is_non_crypto_base("ARB")
          and not screener.is_non_crypto_base("BTC")
          and not screener.is_non_crypto_base("JUP")
          and not screener.is_non_crypto_base("BNB")
          and not screener.is_non_crypto_base("YB"))

    def _boom():
        raise RuntimeError("network down")
    screener.fetch_universe = _boom
    try:
        screener.build_trading_universe(top_n=3)
        raised = False
    except RuntimeError:
        raised = True
    finally:
        screener.fetch_universe = orig_fetch
    check("screener fetch failure raises (callers must fail closed)",
          raised)

    # --- engine level: many simultaneous proposals, caps still bind --
    import data as market
    import engine
    from journal import Journal

    now_ms = int(time.time() * 1000)

    def fake_candles(symbol, interval, limit=None):
        step = 3_600_000 if interval == "1h" else 14_400_000
        candles = []
        for i in range(300):
            p = 100.0 * (1 + 0.001 * ((i % 7) - 3))
            candles.append({"ts": now_ms - (300 - i) * step,
                            "open": p, "high": p * 1.01, "low": p * 0.99,
                            "close": p, "volume": 100.0})
        return candles, "live"

    def always_propose(symbol, snap, votes):
        price = snap["price"]
        return {"symbol": symbol, "direction": "long", "entry": price,
                "stop_loss": round(price * 0.99, 4),
                "take_profit": round(price * 1.02, 4),
                "avg_confidence": 0.9}

    tmp = tempfile.mkdtemp(prefix="alphaswarm_universe_")
    orig_candles, orig_propose = market.get_candles, engine.build_proposal
    orig_symbols, orig_strategy = config.SYMBOLS, config.STRATEGY
    market.get_candles = fake_candles
    engine.build_proposal = always_propose
    try:
        config.SYMBOLS = [f"COIN{i}USDT" for i in range(10)]
        config.STRATEGY = "v1"
        j = Journal(os.path.join(tmp, "wide.db"))
        results = engine.run_cycle(j, base_dir=tmp)
        opened = [r for r in results if r["action"] == "trade_opened"]
        rejected = [r for r in results
                    if r["action"] == "proposal_rejected"]
        check("wide universe: at most 2 positions open (P5 binds)",
              len(opened) == 2 and len(j.open_trades()) == 2,
              f"opened={len(opened)} rejected={len(rejected)}")
        check("wide universe: the rest are rejected by the risk gate",
              len(rejected) == 8 and all(
                  any("P5" in reason for reason in r.get("reasons", []))
                  for r in rejected))
        check("wide universe: per-trade risk stays <= 1% of equity",
              all(t["risk_amount"] <= 0.01 * 10_000 + 0.01
                  for t in j.open_trades()),
              ", ".join(f"{t['risk_amount']:.2f}"
                        for t in j.open_trades()))
        j.close()
    finally:
        market.get_candles, engine.build_proposal = orig_candles, orig_propose
        config.SYMBOLS, config.STRATEGY = orig_symbols, orig_strategy

    # --- botrunner level: journaled universe + fail-closed -----------
    data_dir = os.path.join(tmp, "data")
    os.environ["ALPHASWARM_DATA_DIR"] = data_dir
    import botrunner
    from platform_db import PlatformDB

    pdb = PlatformDB(os.path.join(data_dir, "platform.db"))
    admin = pdb.create_user("wide@example.com", "Wide", "secret9")
    bot = pdb.create_bot(
        name="Wide v1", kind="v1", start_equity=10000.0,
        symbols=["BTCUSDT"], risk_pct=0.01, daily_loss_pct=0.03,
        cycle_minutes=0, created_by=admin["id"],
        universe_mode="screener", universe_size=5)
    pdb.update_bot(bot["id"], status="paused")
    bot = pdb.get_bot(bot["id"])
    botrunner.init_bot_journal(bot)

    screener.fetch_universe = _fake_meta_tickers
    market.get_candles = fake_candles
    engine.build_proposal = always_propose
    try:
        summary = botrunner.run_cycle(pdb, bot)
    finally:
        market.get_candles, engine.build_proposal = orig_candles, orig_propose
        screener.fetch_universe = orig_fetch
    check("screener-mode cycle runs and names its universe",
          "Universe (screener top 5 + majors, 5 coins)" in summary,
          summary[:130])
    journal = botrunner.open_journal(bot)
    urows = [dict(r) for r in journal.conn.execute(
        "SELECT * FROM cycles WHERE symbol='UNIVERSE'")]
    check("universe persisted in the bot journal for traceability",
          len(urows) == 1 and urows[0]["action"] == "universe"
          and "SOLUSDT" in urows[0]["detail"]
          and "NVDABUSDT" not in urows[0]["detail"].split("symbols=")[-1],
          (urows[0]["detail"][:130] if urows else "no UNIVERSE row"))
    check("screener-mode cycle still opens at most 2 trades",
          len(journal.open_trades()) <= 2,
          f"open={len(journal.open_trades())}")
    journal.close()

    screener.fetch_universe = _boom
    try:
        summary = botrunner.run_cycle(pdb, pdb.get_bot(bot["id"]))
    finally:
        screener.fetch_universe = orig_fetch
    journal = botrunner.open_journal(bot)
    frows = [dict(r) for r in journal.conn.execute(
        "SELECT * FROM cycles WHERE action='universe_failed'")]
    check("screener failure: cycle fails closed, opens nothing",
          "failed closed" in summary and len(frows) == 1
          and len(journal.all_trades()) <= 2,
          summary[:130])
    journal.close()


# ===========================================================================
# Part 1c - breakout bot kind (fully mocked, no live network)
# ===========================================================================

def _flat_candles(n, last_close=100.0, step=3_600_000):
    """n hourly candles flat at 100 (high 101 / low 99); the final
    candle closes at last_close."""
    now_h = (int(time.time() * 1000) // step) * step
    out = []
    for i in range(n):
        close = last_close if i == n - 1 else 100.0
        out.append({"ts": now_h - (n - 1 - i) * step, "open": 100.0,
                    "high": max(101.0, close + 0.5), "low": 99.0,
                    "close": close, "volume": 100.0})
    return out


def _htf_candles(n, rising=True):
    step = 14_400_000
    now_h = (int(time.time() * 1000) // 3_600_000) * 3_600_000
    out = []
    for i in range(n):
        close = 100.0 + (0.5 * i if rising else -0.3 * i)
        out.append({"ts": now_h - (n - i) * step, "open": close,
                    "high": close + 1, "low": close - 1,
                    "close": close, "volume": 100.0})
    return out


def breakout_tests():
    import config
    import data as market
    import indicators
    import engine_breakout
    from breakout import breakout_signal
    from htf import HTFTrend
    from journal import Journal

    # --- signal logic, evaluated directly ------------------------------
    candles = _flat_candles(60, last_close=103.0)
    atr = [None] * 9 + [2.0] * 51  # >= 50 defined values for the guard
    prop, reason = breakout_signal(candles, 59, atr, "up")
    check("breakout signal fires long on a fresh Donchian cross",
          prop is not None and reason == "long"
          and prop["entry"] == 103.0, f"reason={reason}")
    check("breakout geometry: SL = entry - 1.5xATR, TP = entry + 2R",
          prop["stop_loss"] == 100.0 and prop["take_profit"] == 109.0,
          f"sl={prop['stop_loss']} tp={prop['take_profit']}")
    prop2, reason2 = breakout_signal(_flat_candles(60), 59, atr, "up")
    check("no fresh cross -> no proposal (inside)",
          prop2 is None and reason2 == "inside", f"reason={reason2}")
    _, reason3 = breakout_signal(candles, 59, atr, "down")
    check("breakout against the 4h trend is blocked (fail closed)",
          reason3 == "trend_block", f"reason={reason3}")
    _, reason4 = breakout_signal(candles, 59, atr, "unknown")
    check("unknown 4h trend blocks the breakout (fail closed)",
          reason4 == "trend_block", f"reason={reason4}")
    hot_atr = [None] * 5 + [1.0] * 54 + [5.0]
    _, reason5 = breakout_signal(candles, 59, hot_atr, "up")
    check("ATR in its top decile blocks entry (volatility guard)",
          reason5 == "vol_guard", f"reason={reason5}")
    _, reason6 = breakout_signal(candles, 59, [None] * 60, "up")
    check("undefined ATR blocks entry (fail closed)",
          reason6 == "no_atr", f"reason={reason6}")

    # --- 4h trend labels -------------------------------------------------
    check("HTF trend: rising 4h closes -> up",
          HTFTrend(_htf_candles(300, rising=True)).at(
              int(time.time() * 1000)) == "up")
    check("HTF trend: falling 4h closes -> down",
          HTFTrend(_htf_candles(300, rising=False)).at(
              int(time.time() * 1000)) == "down")
    check("HTF trend: too little history -> unknown",
          HTFTrend(_htf_candles(10)).at(
              int(time.time() * 1000)) == "unknown")

    # --- engine level: mocked market, one trade, dedupe -----------------
    candles_1h = _flat_candles(300, last_close=103.0)
    candles_4h = _htf_candles(300, rising=True)

    def fake_candles(symbol, interval, limit=None):
        if interval == "4h":
            return list(candles_4h), "live"
        return list(candles_1h), "live"

    def flat_atr(cs, period=14):
        return [None] * 13 + [2.0] * (len(cs) - 13)

    tmp = tempfile.mkdtemp(prefix="alphaswarm_breakout_")
    orig_candles, orig_atr = market.get_candles, indicators.atr_series
    orig_symbols = config.SYMBOLS
    market.get_candles = fake_candles
    indicators.atr_series = flat_atr
    try:
        config.SYMBOLS = ["BTCUSDT"]
        j = Journal(os.path.join(tmp, "breakout.db"))
        results = engine_breakout.run_cycle(j, base_dir=tmp)
        opened = [r for r in results if r["action"] == "trade_opened"]
        trades = j.all_trades()
        check("mocked breakout cycle opens exactly one trade",
              len(opened) == 1 and len(trades) == 1,
              f"opened={len(opened)} trades={len(trades)}")
        t = trades[0]
        check("breakout trade keeps the study geometry (SL 1.5xATR, TP 2R)",
              t["direction"] == "long" and t["entry_price"] == 103.0
              and t["stop_loss"] == 100.0 and t["take_profit"] == 109.0
              and t["tp1_price"] == 106.0,
              f"entry={t['entry_price']} sl={t['stop_loss']} "
              f"tp={t['take_profit']} tp1={t['tp1_price']}")
        check("breakout trade stores a plain-language reason",
              "20 hours" in (t["entry_reason"] or "")
              and "Volatility check passed" in t["entry_reason"]
              and "Paper trade" in t["entry_reason"],
              (t["entry_reason"] or "")[:80])
        check("breakout trade pays the 0.1% entry fee",
              (t["fees_paid"] or 0) > 0, f"fees={t['fees_paid']}")
        results = engine_breakout.run_cycle(j, base_dir=tmp)
        check("same signal bar re-run opens no duplicate trade",
              len(j.all_trades()) == 1
              and not any(r["action"] == "trade_opened" for r in results),
              f"trades={len(j.all_trades())}")
        j.close()

        # Trend down: the same long breakout must not open.
        candles_4h = _htf_candles(300, rising=False)
        j2 = Journal(os.path.join(tmp, "breakout_down.db"))
        results = engine_breakout.run_cycle(j2, base_dir=tmp)
        rows = [dict(r) for r in j2.conn.execute(
            "SELECT detail FROM cycles WHERE action='no_proposal'")]
        check("mocked breakout cycle with 4h trend down opens nothing",
              len(j2.all_trades()) == 0
              and any("trend_block" in (r["detail"] or "") for r in rows),
              f"trades={len(j2.all_trades())}")
        j2.close()
    finally:
        market.get_candles, indicators.atr_series = orig_candles, orig_atr
        config.SYMBOLS = orig_symbols


# ===========================================================================
# Part 1e - Regime Router (classifier unit tests + mocked router cycles)
# ===========================================================================

def _series_candles(closes, wick=0.001):
    """Hourly candles from a close series, timestamps ending at the
    current hour (same alignment as _flat_candles)."""
    step = 3_600_000
    now_h = (int(time.time() * 1000) // step) * step
    out = []
    prev = closes[0]
    for k, c in enumerate(closes):
        o = prev
        out.append({"ts": now_h - (len(closes) - 1 - k) * step,
                    "open": o, "high": max(o, c) * (1 + wick),
                    "low": min(o, c) * (1 - wick), "close": c,
                    "volume": 100.0})
        prev = c
    return out


def _trend_closes(n=300, accelerating=True):
    closes, p = [], 100.0
    for k in range(n):
        if accelerating:
            p *= 1.002 + 0.004 * (k / n)
        else:
            p *= 1.005 - 0.0025 * (k / n)
        closes.append(p)
    return closes


def _range_closes(n=300):
    return [102.0 if (k // 5) % 2 == 0 else 98.0 for k in range(n)]


def _compression_closes(n=300):
    import math
    return [100.0 + 4.0 * (1 - k / n) ** 1.5
            * math.sin(2 * math.pi * k / 16) for k in range(n)]


def _mixed_closes(n=300):
    """Noisy history, then a sharp 10-bar rise fully retraced by a
    10-bar fall inside the measurement window: the path travelled is
    large, the net change ~zero, and trend strength disagrees with
    efficiency - the definition of MIXED_UNCLEAR."""
    import random
    rng = random.Random(7)
    closes = [100.0]
    for _ in range(n - 21):
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.004)))
    base = closes[-1]
    for k in range(1, 11):
        closes.append(base * (1.006 ** k))
    top = closes[-1]
    for k in range(1, 11):
        closes.append(top * (0.994 ** k))
    return closes


def _expansion_candles(n=300):
    """Accelerating uptrend whose closes stay INSIDE the Donchian
    channel (long upper wicks set the channel high) until a final
    thrust bar closes above it: TREND_EXPANSION + a fresh breakout
    cross on the last bar."""
    step = 3_600_000
    now_h = (int(time.time() * 1000) // step) * step
    closes = _trend_closes(n, accelerating=True)
    closes[-1] *= 1.03
    out = []
    prev = closes[0]
    for k, c in enumerate(closes):
        o = prev
        out.append({"ts": now_h - (n - 1 - k) * step, "open": o,
                    "high": c * 1.012, "low": o * 0.999, "close": c,
                    "volume": 100.0})
        prev = c
    return out


def regime_tests():
    import config
    import data as market
    import indicators
    import engine_router
    import regime
    from journal import Journal

    # --- classifier labels on synthetic series -------------------------
    r = regime.classify_regime(_series_candles(_trend_closes()))
    check("regime: accelerating trend is TREND_EXPANSION",
          r["label"] == "TREND_EXPANSION"
          and r["er"] >= 0.5 and r["adx"] >= 25,
          f"{r['label']} {r['measures']}")
    r = regime.classify_regime(
        _series_candles(_trend_closes(accelerating=False)))
    check("regime: steady decelerating trend is CLEAN_TREND",
          r["label"] == "CLEAN_TREND", f"{r['label']} {r['measures']}")
    r = regime.classify_regime(_series_candles(_range_closes()))
    check("regime: square-wave sideways market is RANGING",
          r["label"] == "RANGING" and r["er"] <= 0.25 and r["adx"] < 20,
          f"{r['label']} {r['measures']}")
    r = regime.classify_regime(_series_candles(_compression_closes()))
    check("regime: decaying oscillation is COMPRESSION",
          r["label"] == "COMPRESSION" and r["bbw_pct"] <= 0.20,
          f"{r['label']} {r['measures']}")
    r = regime.classify_regime(_series_candles(_mixed_closes()))
    check("regime: rise fully retraced in-window is MIXED_UNCLEAR",
          r["label"] == "MIXED_UNCLEAR", f"{r['label']} {r['measures']}")
    up = [100.0 + k for k in range(50)]
    check("regime: ER of a straight line is 1, of a square wave is 0",
          regime.efficiency_ratio(up, 49) == 1.0
          and regime.efficiency_ratio(_range_closes(50), 49) == 0.0)
    r = regime.classify_regime(_series_candles(_trend_closes()[:30]))
    check("regime: too little history fails safe to MIXED_UNCLEAR",
          r["label"] == "MIXED_UNCLEAR" and r["bbw_pct"] is None,
          f"{r['label']} {r['measures']}")

    # --- router cycle: sit-out regimes open nothing --------------------
    range_1h = _series_candles(_range_closes())
    mixed_1h = _series_candles(_mixed_closes())
    htf_up = _htf_candles(300, rising=True)
    by_symbol = {"BTCUSDT": range_1h, "ETHUSDT": mixed_1h,
                 "SOLUSDT": range_1h}

    def fake_sitout(symbol, interval, limit=None):
        if interval == "4h":
            return list(htf_up), "live"
        return list(by_symbol[symbol]), "live"

    def flat_atr(cs, period=14):
        return [None] * 13 + [2.0] * (len(cs) - 13)

    tmp = tempfile.mkdtemp(prefix="alphaswarm_regime_")
    orig_candles, orig_atr = market.get_candles, indicators.atr_series
    orig_symbols = config.SYMBOLS
    market.get_candles = fake_sitout
    indicators.atr_series = flat_atr
    try:
        config.SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        j = Journal(os.path.join(tmp, "router_sitout.db"))
        results = engine_router.run_cycle(j, base_dir=tmp)
        rows = [dict(x) for x in j.conn.execute(
            "SELECT symbol, action, detail FROM cycles")]
        regime_rows = [x for x in rows if "regime=" in (x["detail"] or "")]
        check("router: every symbol's regime decision is journaled",
              len(regime_rows) == 3
              and all(x["action"] == "regime_no_trade"
                      for x in regime_rows),
              "; ".join(f"{x['symbol']}:{x['detail'][:40]}"
                        for x in regime_rows))
        check("router: RANGING and MIXED_UNCLEAR open nothing",
              len(j.all_trades()) == 0
              and all(x["action"] == "regime_no_trade" for x in results)
              and any("regime=RANGING" in (x["detail"] or "")
                      for x in regime_rows)
              and any("regime=MIXED_UNCLEAR" in (x["detail"] or "")
                      for x in regime_rows),
              f"trades={len(j.all_trades())}")
        j.close()

        # --- router cycle: TREND_EXPANSION routes to breakout --------
        exp_1h = _expansion_candles()

        def fake_exp(symbol, interval, limit=None):
            if interval == "4h":
                return list(htf_up), "live"
            return list(exp_1h), "live"

        market.get_candles = fake_exp
        config.SYMBOLS = ["BTCUSDT"]
        j = Journal(os.path.join(tmp, "router_expansion.db"))
        results = engine_router.run_cycle(j, base_dir=tmp)
        trades = j.all_trades()
        check("router: TREND_EXPANSION + fresh cross opens one trade",
              len(trades) == 1
              and any(x["action"] == "trade_opened" for x in results),
              f"trades={len(trades)}")
        t = trades[0] if trades else {}
        check("router: routed trade carries the routing explanation",
              "REGIME ROUTER" in (t.get("entry_reason") or "")
              and "TREND EXPANSION" in t["entry_reason"]
              and "Breakout specialist" in t["entry_reason"]
              and "Paper trade" in t["entry_reason"],
              (t.get("entry_reason") or "")[:90])
        rows = [dict(x) for x in j.conn.execute(
            "SELECT action, detail FROM cycles")]
        check("router: the routed decision is journaled with measures",
              any(x["action"] == "trade_opened"
                  and "regime=TREND_EXPANSION" in (x["detail"] or "")
                  and "er=" in x["detail"] and "adx=" in x["detail"]
                  for x in rows))
        engine_router.run_cycle(j, base_dir=tmp)
        check("router: same signal bar re-run opens no duplicate",
              len(j.all_trades()) == 1, f"trades={len(j.all_trades())}")
        j.close()
    finally:
        market.get_candles, indicators.atr_series = orig_candles, orig_atr
        config.SYMBOLS = orig_symbols


# ===========================================================================
# Part 1f - Range Specialist candidate (stochastic range fade)
# ===========================================================================

def _box_range_closes(n=400, seed=42):
    """Mean-reverting noise contained in a box around 100: a genuine
    sideways market - flat trend line, no sustained direction, price
    crossing the box repeatedly (deterministic)."""
    import random
    rng = random.Random(seed)
    closes, p = [], 100.0
    for _ in range(n):
        p += (100.0 - p) * 0.08 + rng.gauss(0, 1.1)
        p = max(94.0, min(106.0, p))
        closes.append(p)
    return closes


def _trend_pullback_closes(n=300):
    """A strong uptrend with one sharp 18-bar pullback: a stochastic
    cross-up fires inside the pullback, but the market is trending,
    so the range gate must refuse it."""
    closes, p = [], 100.0
    for k in range(n):
        p *= 0.988 if 180 <= k < 198 else 1.0045
        closes.append(p)
    return closes


def range_tests():
    import config
    import data as market
    import indicators
    import engine_range
    import range_fade
    from journal import Journal

    # --- stochastic basics ---------------------------------------------
    flat = _series_candles([100.0] * 200)
    flat[0]["high"] = flat[0]["low"] = 100.0
    for c in flat:
        c["high"] = c["low"] = c["close"] = 100.0
    stoch = range_fade.stochastic_series(flat)
    check("range: stochastic of a flat series is defined as 50 (no cross)",
          all(v == 50.0 for v in stoch if v is not None)
          and stoch[-1] == 50.0)

    # --- the gate refuses a trend, even at a stochastic cross ----------
    trend = _series_candles(_trend_pullback_closes())
    atr = indicators.atr_series(trend, 14)
    series = range_fade.build_series(trend)
    gate_opens = sum(1 for i in range(len(trend))
                     if range_fade.range_measures(
                         trend, i, atr, series)["gate_open"])
    check("range: gate stays closed on every bar of a trending series",
          gate_opens == 0, f"gate_open_bars={gate_opens}")
    cross_bars = [i for i in range(1, len(trend))
                  if series["stoch"][i - 1] is not None
                  and series["stoch"][i] is not None
                  and series["stoch"][i - 1] <= 20 < series["stoch"][i]]
    refused = [i for i in cross_bars
               if range_fade.range_signal(trend, i, atr, series)[0] is None
               and range_fade.range_signal(trend, i, atr, series)[1]
               .startswith("gate_")]
    check("range: a stochastic cross inside a trend pullback is refused "
          "by the gate", len(cross_bars) >= 1 and refused == cross_bars,
          f"cross_bars={cross_bars} refused={refused}")

    # --- the gate opens in a range; proposals carry range geometry -----
    box = _series_candles(_box_range_closes())
    atr = indicators.atr_series(box, 14)
    series = range_fade.build_series(box)
    gate_opens = sum(1 for i in range(len(box))
                     if range_fade.range_measures(
                         box, i, atr, series)["gate_open"])
    check("range: gate opens in a sideways box market", gate_opens > 50,
          f"gate_open_bars={gate_opens}/{len(box)}")
    first_long = None
    for i in range(len(box)):
        prop, why = range_fade.range_signal(box, i, atr, series)
        if prop and prop["direction"] == "long":
            first_long = (i, prop)
            break
    check("range: a long proposal fires on a stochastic turn in the box",
          first_long is not None)
    if first_long:
        i, prop = first_long
        hi, _ = range_fade.channel(box, i)
        check("range geometry: SL = entry - 1.5xATR, TP = channel high",
              abs(prop["stop_loss"] - (prop["entry"] - 1.5 * atr[i])) < 0.01
              and abs(prop["take_profit"] - hi) < 0.01,
              f"sl={prop['stop_loss']} tp={prop['take_profit']} hi={hi:.2f}")
        check("range trigger: %K crossed up through 20 at the signal bar",
              prop["stoch_k_prev"] <= 20 < prop["stoch_k"],
              f"{prop['stoch_k_prev']} -> {prop['stoch_k']}")

    # --- mocked cycle: trending market opens nothing, journals sit-outs
    tmp = tempfile.mkdtemp(prefix="alphaswarm_range_")
    orig_candles = market.get_candles
    orig_symbols = config.SYMBOLS

    def fake_trend(symbol, interval, limit=None):
        return list(trend), "live"

    market.get_candles = fake_trend
    try:
        config.SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        j = Journal(os.path.join(tmp, "range_trend.db"))
        results = engine_range.run_cycle(j, base_dir=tmp)
        rows = [dict(x) for x in j.conn.execute(
            "SELECT symbol, action, detail FROM cycles")]
        sitouts = [x for x in rows if x["action"] == "range_no_trade"]
        check("range cycle: trending market opens nothing",
              len(j.all_trades()) == 0
              and all(x["action"] == "range_no_trade" for x in results),
              f"trades={len(j.all_trades())}")
        check("range cycle: every gate-closed sit-out is journaled with "
              "its measures", len(sitouts) == 3
              and all("range gate CLOSED" in (x["detail"] or "")
                      and "adx=" in x["detail"] for x in sitouts),
              "; ".join(f"{x['symbol']}:{(x['detail'] or '')[:40]}"
                        for x in sitouts))
        j.close()

        # --- mocked cycle: ranging market, last bar is a turn ---------
        last_sig = None
        for i in range(len(box)):
            prop, why = range_fade.range_signal(box, i, atr, series)
            if prop and prop["direction"] == "long":
                last_sig = i
        box_trunc = box[:last_sig + 1]
        # Re-stamp so the truncated series ENDS at the current hour
        # (the full series' timestamps run backwards from now, so a
        # raw truncation would end hours in the past and the risk
        # policy would - correctly - refuse the data as stale).
        step = 3_600_000
        now_h = (int(time.time() * 1000) // step) * step
        shift = now_h - box_trunc[-1]["ts"]
        box_trunc = [dict(c, ts=c["ts"] + shift) for c in box_trunc]

        def fake_box(symbol, interval, limit=None):
            return list(box_trunc), "live"

        market.get_candles = fake_box
        config.SYMBOLS = ["BTCUSDT"]
        j = Journal(os.path.join(tmp, "range_box.db"))
        results = engine_range.run_cycle(j, base_dir=tmp)
        trades = j.all_trades()
        check("range cycle: a turn on the last bar opens exactly one trade",
              len(trades) == 1
              and any(x["action"] == "trade_opened" for x in results),
              f"trades={len(trades)}")
        t = trades[0] if trades else {}
        check("range cycle: the trade's reason states the sideways "
              "evidence and the CANDIDATE status",
              "SIDEWAYS" in (t.get("entry_reason") or "")
              and "CANDIDATE STRATEGY - NOT YET PROVEN"
              in t["entry_reason"]
              and "Paper trade" in t["entry_reason"],
              (t.get("entry_reason") or "")[:90])
        check("range cycle: trade pays the 0.1% entry fee",
              (t.get("fees_paid") or 0) > 0, f"fees={t.get('fees_paid')}")
        engine_range.run_cycle(j, base_dir=tmp)
        check("range cycle: same signal bar re-run opens no duplicate",
              len(j.all_trades()) == 1, f"trades={len(j.all_trades())}")
        j.close()
    finally:
        market.get_candles = orig_candles
        config.SYMBOLS = orig_symbols


# ===========================================================================
# Part 1g - live signals for the signal-first home (fully mocked data)
# ===========================================================================

def signals_tests():
    import data as market
    import indicators
    import screener
    import signals

    orig_candles = market.get_candles
    orig_atr = indicators.atr_series
    orig_fetch = screener.fetch_universe
    orig_bp, orig_fp = signals.build_proposal, signals.filter_proposal

    def reset_cache():
        signals._single_cache.clear()
        signals._overview_cache = None

    htf_up = _htf_candles(300, rising=True)
    range_1h = _series_candles(_range_closes())

    def fake_range(symbol, interval, limit=None):
        if interval == "4h":
            return list(htf_up), "live"
        return list(range_1h), "live"

    # --- ranging market: WAIT, sit out, no invented levels -------------
    reset_cache()
    market.get_candles = fake_range
    try:
        card = signals.get_signal("BTCUSDT")
    finally:
        market.get_candles = orig_candles
    check("signals: ranging market answers WAIT and sits out",
          card["action"] == "WAIT" and card["regime"] == "RANGING"
          and card["entry"] is None and card["take_profit"] is None
          and card["stop_loss"] is None
          and "sits out" in " ".join(card["reasons"]),
          f"{card['action']} {card['regime']}")
    check("signals: WAIT card still carries the live price + timestamp",
          card["source"] == "live" and card["price"] is not None
          and card["as_of"] == range_1h[-1]["ts"],
          f"price={card['price']} as_of={card['as_of']}")

    # --- sample-data fallback must be refused (fail closed) ------------
    def fake_sample(symbol, interval, limit=None):
        return list(range_1h), "sample"

    reset_cache()
    market.get_candles = fake_sample
    try:
        card = signals.get_signal("ETHUSDT")
    finally:
        market.get_candles = orig_candles
    check("signals: sample-data fallback is refused (fail closed)",
          card["action"] == "WAIT" and card["source"] == "unavailable"
          and "error" in card and card["entry"] is None
          and "never invents prices" in " ".join(card["reasons"]),
          card.get("error", "")[:60])

    # --- trend expansion: breakout specialist fires --------------------
    exp_1h = _expansion_candles()

    def fake_exp(symbol, interval, limit=None):
        if interval == "4h":
            return list(htf_up), "live"
        return list(exp_1h), "live"

    def flat_atr(cs, period=14):
        return [None] * 13 + [2.0] * (len(cs) - 13)

    reset_cache()
    market.get_candles = fake_exp
    indicators.atr_series = flat_atr
    try:
        card = signals.get_signal("SOLUSDT")
    finally:
        market.get_candles = orig_candles
        indicators.atr_series = orig_atr
    check("signals: trend expansion + fresh cross answers BUY",
          card["action"] == "BUY" and card["regime"] == "TREND_EXPANSION"
          and card["strategy"].startswith("Breakout")
          and card["entry"] is not None
          and card["take_profit"] > card["entry"] > card["stop_loss"],
          f"{card['action']} entry={card['entry']} "
          f"tp={card['take_profit']} sl={card['stop_loss']}")
    check("signals: breakout card shows no fabricated vote confidence",
          card["confidence"] is None
          and any("rule events" in r for r in card["reasons"]))

    # --- clean trend: consensus proposal passes the v2 filter ----------
    trend_1h = _series_candles(_trend_closes(accelerating=False))

    def fake_trend(symbol, interval, limit=None):
        if interval == "4h":
            return list(htf_up), "live"
        return list(trend_1h), "live"

    def fake_proposal(sym, snap, votes):
        return {"symbol": sym, "direction": "long",
                "entry": snap["price"],
                "stop_loss": round(snap["price"] * 0.99, 4),
                "take_profit": round(snap["price"] * 1.02, 4),
                "avg_confidence": 0.72,
                "agreeing_agents": ["TrendAgent", "MomentumAgent",
                                    "VolatilityAgent"],
                "votes": []}

    reset_cache()
    market.get_candles = fake_trend
    signals.build_proposal = fake_proposal
    signals.filter_proposal = lambda p, s, v: (True, None)
    try:
        card = signals.get_signal("ADAUSDT")
    finally:
        market.get_candles = orig_candles
    check("signals: clean-trend consensus answers BUY with confidence",
          card["action"] == "BUY" and card["regime"] == "CLEAN_TREND"
          and card["confidence"] == 0.72
          and card["strategy"].startswith("Analyst consensus"),
          f"{card['action']} conf={card['confidence']}")

    signals.filter_proposal = lambda p, s, v: (False, "v2_htf_filter")
    reset_cache()
    market.get_candles = fake_trend
    try:
        card = signals.get_signal("ADAUSDT")
    finally:
        market.get_candles = orig_candles
        signals.build_proposal, signals.filter_proposal = orig_bp, orig_fp
    check("signals: v2 filter block answers WAIT with the plain reason",
          card["action"] == "WAIT" and card["entry"] is None
          and any("fights the 4-hour" in r for r in card["reasons"]),
          card["reasons"][1][:70] if len(card["reasons"]) > 1 else "")

    # --- cache: a repeat lookup does not re-fetch ----------------------
    reset_cache()
    calls = {"n": 0}

    def counting(symbol, interval, limit=None):
        calls["n"] += 1
        return fake_range(symbol, interval, limit)

    market.get_candles = counting
    try:
        signals.get_signal("XRPUSDT")
        n_first = calls["n"]
        signals.get_signal("XRPUSDT")
    finally:
        market.get_candles = orig_candles
    check("signals: repeat lookup inside the TTL comes from cache",
          n_first == 2 and calls["n"] == 2,
          f"fetches={calls['n']}")

    # --- overview: top screener markets, non-crypto excluded ----------
    reset_cache()
    screener.fetch_universe = _fake_meta_tickers
    market.get_candles = fake_range
    try:
        payload = signals.get_signals()
    finally:
        screener.fetch_universe = orig_fetch
        market.get_candles = orig_candles
    syms = [c["symbol"] for c in payload["signals"]]
    check("signals overview: capped top markets, non-crypto excluded",
          payload["error"] is None and 0 < len(syms) <= 10
          and not ({"NVDABUSDT", "XAUTUSDT", "SOXLBUSDT"} & set(syms)),
          ", ".join(syms))
    check("signals overview: every card is a complete signal card",
          all("action" in c and "reasons" in c and "as_of" in c
              and "regime" in c for c in payload["signals"]))

    def _boom():
        raise RuntimeError("network down")

    screener.fetch_universe = _boom
    signals._overview_cache = None
    try:
        payload = signals.get_signals()
    finally:
        screener.fetch_universe = orig_fetch
    check("signals overview: screener failure fails closed, no cards",
          payload["signals"] == [] and bool(payload["error"]),
          (payload["error"] or "")[:60])
    reset_cache()


# ===========================================================================
# Part 1h - Tradebise v3: live-price overlay, watchlist + quota storage
# ===========================================================================

def tradebise_tests():
    import data as market
    import screener
    import signals
    from platform_db import PLANS, PlatformDB

    orig_candles = market.get_candles
    orig_live = signals.fetch_live_price
    orig_fetch = screener.fetch_universe

    def reset_cache():
        signals._single_cache.clear()
        signals._overview_cache = None

    htf_up = _htf_candles(300, rising=True)
    range_1h = _series_candles(_range_closes())

    def fake_range(symbol, interval, limit=None):
        if interval == "4h":
            return list(htf_up), "live"
        return list(range_1h), "live"

    # --- live price overlay: fresh ticker on the cached signal ---------
    reset_cache()
    market.get_candles = fake_range
    signals.fetch_live_price = lambda sym: 123.45
    try:
        card = signals.get_signal("BTCUSDT")
    finally:
        market.get_candles = orig_candles
        signals.fetch_live_price = orig_live
    check("v3: live_price overlay shows the fresh ticker price",
          card["live_price"] == 123.45 and card["source"] == "live"
          and card["price"] == range_1h[-1]["close"],
          f"live={card.get('live_price')} signal={card.get('price')}")
    cached = signals._single_cache["BTCUSDT"][1]
    check("v3: overlay never mutates the cached signal card",
          cached["live_price"] is None and cached is not card)

    seen = []

    def fake_live(sym):
        seen.append(sym)
        return 200.0 + len(seen)

    signals.fetch_live_price = fake_live
    try:
        again = signals.get_signal("BTCUSDT")
    finally:
        signals.fetch_live_price = orig_live
    check("v3: repeat lookup re-fetches the live price fresh",
          again["live_price"] == 201.0 and seen == ["BTCUSDT"],
          f"live={again.get('live_price')}")

    # --- ticker failure -> live_price None, signal unharmed -------------
    reset_cache()
    market.get_candles = fake_range
    signals.fetch_live_price = lambda sym: None
    try:
        card = signals.get_signal("ETHUSDT")
    finally:
        market.get_candles = orig_candles
        signals.fetch_live_price = orig_live
    check("v3: ticker failure leaves live_price null, signal intact",
          card["live_price"] is None and card["action"] == "WAIT"
          and card["price"] is not None,
          f"live={card.get('live_price')} action={card['action']}")

    # --- overview: overlay re-fetched per call, cache never mutated ----
    reset_cache()
    screener.fetch_universe = _fake_meta_tickers
    market.get_candles = fake_range
    ticks = {"n": 0}

    def counting_live(sym):
        ticks["n"] += 1
        return 500.0 + ticks["n"]

    signals.fetch_live_price = counting_live
    try:
        p1 = signals.get_signals()
        n_after_first = ticks["n"]
        snap = [c["live_price"]
                for c in signals._overview_cache[1]["signals"]]
        p2 = signals.get_signals()
        snap2 = [c["live_price"]
                 for c in signals._overview_cache[1]["signals"]]
    finally:
        screener.fetch_universe = orig_fetch
        market.get_candles = orig_candles
        signals.fetch_live_price = orig_live
    check("v3: overview cards carry a fresh live price on every call",
          bool(p1["signals"])
          and all(c["live_price"] is not None for c in p2["signals"])
          and [c["live_price"] for c in p2["signals"]] != snap
          and ticks["n"] > n_after_first,
          f"ticker calls={ticks['n']}")
    check("v3: cached overview cards are never mutated by the overlay",
          snap == snap2 and len(snap) == len(p1["signals"]))
    reset_cache()

    # --- watchlist + signal usage storage -------------------------------
    tmp = tempfile.mkdtemp(prefix="alphaswarm_v3_")
    pdb = PlatformDB(os.path.join(tmp, "platform.db"))
    u = pdb.create_user("watch@example.com", "Watch", "secret9")
    pdb.watchlist_add(u["id"], "BTCUSDT")
    pdb.watchlist_add(u["id"], "ETHUSDT")
    pdb.watchlist_add(u["id"], "BTCUSDT")  # duplicate must be ignored
    check("v3: watchlist add + list (duplicates ignored)",
          pdb.watchlist_list(u["id"]) == ["BTCUSDT", "ETHUSDT"],
          ", ".join(pdb.watchlist_list(u["id"])))
    pdb.watchlist_remove(u["id"], "BTCUSDT")
    check("v3: watchlist remove",
          pdb.watchlist_list(u["id"]) == ["ETHUSDT"])
    day = "2026-11-21"
    check("v3: signal usage counts per user per UTC day",
          pdb.signal_usage_count(u["id"], day) == 0
          and pdb.signal_usage_bump(u["id"], day) == 1
          and pdb.signal_usage_bump(u["id"], day) == 2
          and pdb.signal_usage_count(u["id"], "2026-11-22") == 0)
    check("v3: plan copy advertises the signal quota honestly",
          any("2 live signal searches a day" in f
              for f in PLANS["free"]["features"])
          and any("Unlimited live signal searches" in f
                  for f in PLANS["pro"]["features"]))


# ===========================================================================
# Part 1d - platform hardening (backups, scheduler isolation, legal)
# ===========================================================================

def hardening_tests():
    import backup as backup_service
    from journal import Journal
    from platform_db import PlatformDB

    tmp = tempfile.mkdtemp(prefix="alphaswarm_hardening_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(os.path.join(data_dir, "bots"), exist_ok=True)
    pdb = PlatformDB(os.path.join(data_dir, "platform.db"))
    user = pdb.create_user("bak@example.com", "Bak", "secret9")
    bot = pdb.create_bot(name="BakBot", kind="v1", start_equity=10000.0,
                         symbols=["BTCUSDT"], risk_pct=0.01,
                         daily_loss_pct=0.03, cycle_minutes=0,
                         created_by=user["id"])
    jpath = os.path.join(data_dir, "bots", str(bot["id"]), "journal.db")
    os.makedirs(os.path.dirname(jpath), exist_ok=True)
    jj = Journal(jpath)
    jj.log_equity(10000.0, "test")
    jj.close()

    folder, files = backup_service.backup_once(data_dir)
    check("backup creates today's dated folder with platform DB",
          os.path.basename(folder) == time.strftime("%Y-%m-%d")
          and "platform.db" in files
          and os.path.exists(os.path.join(folder, "platform.db")),
          folder)
    check("backup includes every bot journal",
          os.path.join("bots", str(bot["id"]), "journal.db") in files
          and os.path.exists(os.path.join(
              folder, "bots", str(bot["id"]), "journal.db")),
          ", ".join(files))
    name, blob = backup_service.latest_backup_zip(data_dir)
    check("latest backup downloads as a zip",
          name.endswith(".zip") and blob[:2] == b"PK" and len(blob) > 100,
          f"{name} {len(blob)} bytes")

    # Retention: 9 extra old dated folders -> only newest 7 survive.
    root = os.path.join(data_dir, "backups")
    for i in range(1, 10):
        old = os.path.join(root, f"2020-01-{i:02d}")
        os.makedirs(old, exist_ok=True)
        with open(os.path.join(old, "dummy"), "w") as f:
            f.write("x")
    backup_service.backup_once(data_dir)
    dated = [d for d in os.listdir(root)
             if os.path.isdir(os.path.join(root, d))]
    check("backup retention keeps only the newest 7 days",
          len(dated) == 7 and "2020-01-01" not in dated,
          f"{len(dated)} folders kept")

    # --- scheduler fault isolation --------------------------------------
    import botrunner

    class FakeDB:
        def __init__(self, bots):
            self._bots = bots

        def get_setting(self, key):
            return "0"

        def list_bots(self):
            return self._bots

        def get_bot(self, bot_id):
            return next(b for b in self._bots if b["id"] == bot_id)

    now = int(time.time() * 1000)
    fake_bots = [
        {"id": 1, "status": "running", "cycle_minutes": 1,
         "last_cycle_at": 0},
        {"id": 2, "status": "running", "cycle_minutes": 1,
         "last_cycle_at": 0},
        {"id": 3, "status": "paused", "cycle_minutes": 1,
         "last_cycle_at": 0},
    ]
    ran = []
    orig_run_cycle = botrunner.run_cycle

    def flaky_run_cycle(db_, bot_):
        if bot_["id"] == 1:
            raise RuntimeError("injected cycle failure")
        ran.append(bot_["id"])
        return "ok"

    botrunner.run_cycle = flaky_run_cycle
    try:
        botrunner.scheduler_tick(FakeDB(fake_bots))
    finally:
        botrunner.run_cycle = orig_run_cycle
    check("scheduler: one bot's exception cannot skip the other bots",
          ran == [2], f"ran={ran} (bot 1 raised, bot 3 paused)")

    # --- legal pages + log rotation wiring -------------------------------
    with open(os.path.join(BASE, "static", "index.html"),
              encoding="utf-8") as f:
        spa = f.read()
    check("SPA has Terms of Service and Privacy Policy pages",
          'id="tab-terms"' in spa and 'id="tab-privacy"' in spa
          and "Terms of Service" in spa and "Privacy Policy" in spa)
    check("legal pages state the paper-only / no-guarantee basics",
          "no profit guarantee" in spa.lower()
          and "paper-trading" in spa.lower()
          and "TEST MODE" in spa)
    with open(os.path.join(BASE, "app.py"), encoding="utf-8") as f:
        app_src = f.read()
    check("access log is capped by rotation and backups run in-app",
          "RotatingFileHandler" in app_src and "maxBytes" in app_src
          and "backup_loop" in app_src)


# ===========================================================================
# Part 2 - HTTP acceptance flow
# ===========================================================================

class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, method, path, body=None, timeout=120):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}


def http_tests():
    port = 8123
    data_dir = tempfile.mkdtemp(prefix="alphaswarm_accept_")
    env = dict(os.environ, ALPHASWARM_PORT=str(port),
               ALPHASWARM_DATA_DIR=data_dir)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "app.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/plans", timeout=2)
                break
            except Exception:
                time.sleep(0.4)
        admin = Client(base)
        user = Client(base)

        st, d = admin.call("POST", "/api/signup",
                           {"email": "owner@example.com", "name": "Owner",
                            "password": "secret1"})
        check("first signup succeeds and becomes ADMIN",
              st == 200 and d["user"]["role"] == "admin",
              f"role={d.get('user', {}).get('role')}")

        st, d = user.call("POST", "/api/signup",
                          {"email": "sub@example.com", "name": "Sub",
                           "password": "secret2"})
        check("second signup is a normal user on Free plan",
              st == 200 and d["user"]["role"] == "user"
              and d["user"]["plan"] == "free")

        st, d = user.call("GET", "/api/news")
        check("news feed is plan-gated for Free users", st == 403,
              f"status={st}")

        st, d = admin.call("POST", "/api/bots",
                           {"name": "Technical v1", "kind": "v1"})
        bot_id = d["bot"]["id"]
        check("admin creates a v1 bot", st == 200 and bot_id > 0,
              f"bot #{bot_id}")

        st, d = admin.call("POST", f"/api/bots/{bot_id}/cycle", timeout=300)
        check("v1 bot runs a live cycle without crashing",
              st == 200 and "Cycle done" in d.get("summary", ""),
              d.get("summary", "")[:110])

        st, d = admin.call("POST", f"/api/bots/{bot_id}/demo-trade",
                           timeout=300)
        if not d.get("ok"):  # natural trades may already fill both slots
            st2, open_d = admin.call("GET", "/api/trades/open")
            for t in open_d.get("trades", []):
                if t["bot_id"] == bot_id:
                    admin.call("POST",
                               f"/api/trades/{bot_id}/{t['id']}/close",
                               timeout=300)
                    break
            st, d = admin.call("POST", f"/api/bots/{bot_id}/demo-trade",
                               timeout=300)
        check("test position opens through the risk gate",
              d.get("ok") is True, d.get("detail", "")[:110])
        trade_id = d.get("trade_id")

        st, d = admin.call("GET", f"/api/bots/{bot_id}")
        trades = d.get("trades", [])
        mine = [t for t in trades if t["id"] == trade_id]
        check("test trade stored with SL, TP, TP1 and a plain reason",
              bool(mine) and mine[0]["stop_loss"] is not None
              and mine[0]["take_profit"] is not None
              and mine[0]["tp1_price"] is not None
              and "TEST POSITION" in (mine[0].get("entry_reason") or ""),
              (mine[0].get("entry_reason") or "")[:80] if mine else "missing")
        check("bot detail API exposes per-trade reasons to admin",
              d.get("reasons_visible") is True)

        st, d = admin.call("POST", f"/api/bots/{bot_id}/kill")
        check("kill switch closes open positions",
              st == 200 and len(d.get("closed", [])) >= 1,
              f"closed={len(d.get('closed', []))}")
        st, d = admin.call("GET", "/api/trades/open")
        check("no open trades remain after kill switch",
              all(t["bot_id"] != bot_id for t in d.get("trades", [])))

        st, d = admin.call("POST", "/api/bots",
                           {"name": "Technical v2", "kind": "v2"})
        v2_id = d["bot"]["id"]
        st, d = admin.call("POST", f"/api/bots/{v2_id}/cycle", timeout=300)
        check("v2 bot runs a live cycle without crashing",
              st == 200 and "Cycle done" in d.get("summary", ""),
              d.get("summary", "")[:110])

        st, d = admin.call("POST", "/api/bots",
                           {"name": "Event Watcher", "kind": "event"})
        ev_id = d["bot"]["id"]
        st, d = admin.call("POST", f"/api/bots/{ev_id}/cycle", timeout=600)
        check("event watcher cycle polls news + screener",
              st == 200 and "News:" in d.get("summary", ""),
              d.get("summary", "")[:140])

        st, d = admin.call("POST", f"/api/bots/{bot_id}/settings",
                           {"risk_pct": 0.02, "daily_loss_pct": 0.04,
                            "cycle_minutes": 60,
                            "symbols": ["BTCUSDT", "ETHUSDT"]})
        check("bot settings update (coins, risk, daily loss, schedule)",
              st == 200 and d["bot"]["risk_pct"] == 0.02
              and d["bot"]["symbols"] == ["BTCUSDT", "ETHUSDT"])

        st, d = admin.call("POST", f"/api/bots/{bot_id}/settings",
                           {"universe_mode": "screener",
                            "universe_size": 10})
        check("bot settings update (universe mode + size)",
              st == 200 and d["bot"]["universe_mode"] == "screener"
              and d["bot"]["universe_size"] == 10,
              f"mode={d.get('bot', {}).get('universe_mode')} "
              f"size={d.get('bot', {}).get('universe_size')}")
        st, d = admin.call("POST", f"/api/bots/{bot_id}/settings",
                           {"universe_size": 99})
        check("universe size outside 5-30 is rejected", st == 400,
              f"status={st}")
        st, d = admin.call("POST", f"/api/bots/{bot_id}/settings",
                           {"universe_mode": "fixed", "universe_size": 20})
        check("universe settings restore to fixed mode",
              st == 200 and d["bot"]["universe_mode"] == "fixed")

        st, d = user.call("POST", "/api/checkout",
                          {"plan": "pro",
                           "card_number": "4242 4242 4242 4242"})
        check("TEST-MODE checkout succeeds and activates Pro",
              st == 200 and d.get("test_mode") is True
              and d["user"]["plan"] == "pro",
              d.get("message", "")[:90])

        st, d = user.call("GET", "/api/news")
        news = d.get("news", [])
        check("subscriber can now read the news feed (seeded real events)",
              st == 200 and len(news) > 0
              and all("confirmation" in n and "impact_label" in n
                      for n in news),
              f"{len(news)} events")
        check("news carries CONFIRMED and UNCONFIRMED badges",
              any(n["confirmation"] == "CONFIRMED" for n in news)
              and any(n["confirmation"] == "UNCONFIRMED" for n in news))

        st, d = admin.call("POST", f"/api/bots/{bot_id}/publish")
        st, d = user.call("GET", "/api/public/bots")
        check("published bot is visible to subscribers",
              any(b["id"] == bot_id for b in d.get("bots", [])))
        st, d = user.call("GET", f"/api/bots/{bot_id}")
        sub_trades = d.get("trades", [])
        check("subscriber (Pro) sees per-trade reasons on published bot",
              d.get("reasons_visible") is True and bool(sub_trades)
              and "TEST POSITION" in (sub_trades[0].get("entry_reason")
                                      or ""))

        st, d = admin.call("POST", "/api/kill/global")
        check("global kill switch activates", st == 200
              and d.get("ok") is True)
        st, d = admin.call("POST", f"/api/bots/{v2_id}/cycle")
        check("global kill switch blocks further cycles",
              "Global kill switch" in d.get("summary", ""),
              d.get("summary", "")[:80])
        st, d = admin.call("POST", "/api/kill/global/clear")
        check("global kill switch clears", st == 200 and d.get("ok"))

        st, d = admin.call("GET", "/api/compare")
        check("comparison screen data covers all bots",
              st == 200 and len(d.get("bots", [])) == 3
              and all("sharpe" in b and "profit_factor" in b
                      for b in d["bots"]),
              f"{len(d.get('bots', []))} bots")
        st, d = admin.call("GET", "/api/admin/users")
        check("admin panel lists users", st == 200
              and len(d.get("users", [])) == 2)
        st, d = user.call("GET", "/api/admin/users")
        check("admin panel is admin-only", st == 403)

        st, d = admin.call("POST", "/api/bots",
                           {"name": "Technical v1 - All Coins",
                            "kind": "v1", "universe_mode": "screener",
                            "universe_size": 15})
        check("admin creates a screener-universe bot",
              st == 200 and d["bot"]["universe_mode"] == "screener"
              and d["bot"]["universe_size"] == 15,
              f"bot #{d.get('bot', {}).get('id')}")
        st, d = admin.call("POST", "/api/bots",
                           {"name": "Bad size", "kind": "v1",
                            "universe_mode": "screener",
                            "universe_size": 3})
        check("bot creation rejects universe size outside 5-30",
              st == 400, f"status={st}")

        # --- breakout bot kind, end to end ------------------------------
        st, d = admin.call("POST", "/api/bots",
                           {"name": "Breakout - Leading Candidate",
                            "kind": "breakout"})
        bk_id = d.get("bot", {}).get("id")
        check("admin creates a breakout bot",
              st == 200 and bk_id and d["bot"]["kind"] == "breakout",
              f"bot #{bk_id}")
        st, d = admin.call("POST", "/api/bots",
                           {"name": "Nope", "kind": "nope"})
        check("unknown bot kind is still rejected", st == 400,
              f"status={st}")
        st, d = admin.call("GET", f"/api/bots/{bk_id}")
        check("breakout bot detail loads like other kinds",
              st == 200 and d["bot"]["kind"] == "breakout"
              and "equity_curve" in d and "trades" in d)
        st, d = admin.call("POST", f"/api/bots/{bk_id}/cycle",
                           timeout=300)
        check("breakout bot runs a live cycle without crashing",
              st == 200 and "Cycle done" in d.get("summary", ""),
              d.get("summary", "")[:110])
        st, d = admin.call("GET", "/api/compare")
        check("comparison covers the breakout bot too",
              st == 200 and any(b["id"] == bk_id
                                for b in d.get("bots", [])),
              f"{len(d.get('bots', []))} bots")
        st, d = admin.call("POST", f"/api/bots/{bk_id}/demo-trade")
        check("breakout bot refuses agent-based test positions",
              st == 400, f"status={st}")

        # --- regime router bot kind, end to end ------------------------
        st, d = admin.call("POST", "/api/bots",
                           {"name": "Regime Router", "kind": "router"})
        rr_id = d.get("bot", {}).get("id")
        check("admin creates a regime router bot",
              st == 200 and rr_id and d["bot"]["kind"] == "router",
              f"bot #{rr_id}")
        st, d = admin.call("POST", f"/api/bots/{rr_id}/cycle",
                           timeout=300)
        check("regime router runs a live cycle without crashing",
              st == 200 and "Cycle done" in d.get("summary", "")
              and "Regimes:" in d.get("summary", ""),
              d.get("summary", "")[:140])
        st, d = admin.call("POST", f"/api/bots/{rr_id}/demo-trade")
        check("regime router refuses direct test positions",
              st == 400, f"status={st}")

        # --- range specialist candidate bot kind, end to end ------------
        st, d = admin.call("POST", "/api/bots",
                           {"name": "Range Specialist — Candidate "
                                    "(forward proof pending)",
                            "kind": "range"})
        rg_id = d.get("bot", {}).get("id")
        check("admin creates a range specialist candidate bot",
              st == 200 and rg_id and d["bot"]["kind"] == "range",
              f"bot #{rg_id}")
        st, d = admin.call("POST", f"/api/bots/{rg_id}/cycle",
                           timeout=300)
        check("range candidate runs a live cycle without crashing",
              st == 200 and "Cycle done" in d.get("summary", "")
              and "sat out" in d.get("summary", ""),
              d.get("summary", "")[:140])
        st, d = admin.call("POST", f"/api/bots/{rg_id}/demo-trade")
        check("range candidate refuses agent-based test positions",
              st == 400, f"status={st}")

        # --- live signals (signal-first home) ---------------------------
        anon = Client(base)
        st, _ = anon.call("GET", "/api/signal?symbol=BTCUSDT")
        check("HTTP: /api/signal requires a login", st == 401, f"{st}")
        st, sig = admin.call("GET", "/api/signal?symbol=BTCUSDT",
                             timeout=300)
        check("HTTP: /api/signal returns a complete signal card",
              st == 200 and sig.get("symbol") == "BTCUSDT"
              and sig.get("action") in ("BUY", "SELL", "WAIT")
              and "reasons" in sig and "as_of" in sig
              and "regime" in sig and "strategy" in sig
              and "disclaimer" in sig
              and (sig["action"] == "WAIT"
                   or (sig.get("entry") is not None
                       and sig.get("take_profit") is not None
                       and sig.get("stop_loss") is not None)),
              f"{st} {sig.get('action')} src={sig.get('source')}")
        st, _ = admin.call("GET", "/api/signal")
        check("HTTP: /api/signal without a symbol is a 400", st == 400,
              f"{st}")
        st, ov = admin.call("GET", "/api/signals", timeout=300)
        check("HTTP: /api/signals returns capped overview cards "
              "(or an honest fail-closed error)",
              st == 200 and isinstance(ov.get("signals"), list)
              and len(ov["signals"]) <= 10
              and (ov["signals"] or ov.get("error")),
              f"{st} cards={len(ov.get('signals') or [])} "
              f"err={'yes' if ov.get('error') else 'no'}")
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            html = resp.read().decode()
            st = resp.status
        check("HTTP: home page is the signal-first experience",
              st == 200 and 'id="sigSymbol"' in html
              and 'id="sigGrid"' in html and 'id="homeBots"' in html
              and "Advanced" in html and "no guaranteed profits" in html,
              f"{st}")

        # --- Tradebise v3: watchlist, track record, signal quota --------
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            html = resp.read().decode()
        check("HTTP: home carries watchlist + track record sections "
              "and the honest live-price labels",
              'id="watchList"' in html and 'id="recordList"' in html
              and "wins and losses alike" in html
              and "Signal price (last completed hour)" in html
              and "Price now" not in html)

        anon3 = Client(base)
        st, _ = anon3.call("GET", "/api/watchlist")
        check("HTTP: /api/watchlist requires a login", st == 401,
              f"{st}")
        st, _ = anon3.call("GET", "/api/record")
        check("HTTP: /api/record requires a login", st == 401, f"{st}")
        st, _ = anon3.call("POST", "/api/watchlist",
                           {"symbol": "BTCUSDT"})
        check("HTTP: watchlist add requires a login", st == 401,
              f"{st}")

        st, d = user.call("POST", "/api/watchlist", {"symbol": "btc"})
        check("HTTP: watchlist add normalises the symbol",
              st == 200 and d.get("symbols") == ["BTCUSDT"],
              f"{st} {d.get('symbols')}")
        st, d = user.call("GET", "/api/watchlist", timeout=300)
        check("HTTP: watchlist returns symbols with their signal cards",
              st == 200 and d.get("symbols") == ["BTCUSDT"]
              and len(d.get("signals", [])) == 1
              and d["signals"][0]["symbol"] == "BTCUSDT"
              and "live_price" in d["signals"][0]
              and "action" in d["signals"][0],
              f"{st} cards={len(d.get('signals', []))}")
        st, d = user.call("POST", "/api/watchlist", {"symbol": "!!!"})
        check("HTTP: watchlist rejects an invalid symbol", st == 400,
              f"{st}")
        st, d = user.call("POST", "/api/watchlist/remove",
                          {"symbol": "BTCUSDT"})
        check("HTTP: watchlist remove empties the list",
              st == 200 and d.get("symbols") == [],
              f"{st} {d.get('symbols')}")

        # Track record: seed closed trades straight into the bot
        # journals - a winner AND a loser on a published bot, plus a
        # closed trade on an unpublished bot that must NOT appear.
        from journal import Journal

        def seed_closed(bot_id, symbol, direction, entry, exit_price,
                        pnl, reason, exit_ts):
            path = os.path.join(data_dir, "bots", str(bot_id),
                                "journal.db")
            jj = Journal(path)
            prop = {"symbol": symbol, "direction": direction,
                    "entry": entry, "stop_loss": entry * 0.99,
                    "take_profit": entry * 1.02}
            tid = jj.open_trade(0, prop, qty=1.0, risk_amount=10.0,
                                entry_ts_ms=exit_ts - 3_600_000,
                                entry_reason="track-record fixture")
            jj.close_trade(tid, exit_ts, exit_price, reason, pnl)
            jj.close()

        st, d = admin.call("POST", "/api/bots",
                           {"name": "Record Pub", "kind": "v1"})
        rec_pub = d["bot"]["id"]
        st, d = admin.call("POST", "/api/bots",
                           {"name": "Record Hidden", "kind": "v1"})
        rec_hidden = d["bot"]["id"]
        admin.call("POST", f"/api/bots/{rec_pub}/publish")
        base_ts = 1_795_000_000_000
        seed_closed(rec_pub, "BTCUSDT", "long", 100.0, 110.0, 25.5,
                    "take_profit", base_ts + 2_000)
        seed_closed(rec_pub, "ETHUSDT", "short", 50.0, 55.0, -40.25,
                    "stop_loss", base_ts + 3_000)
        seed_closed(rec_hidden, "SOLUSDT", "long", 10.0, 20.0, 999.0,
                    "take_profit", base_ts + 4_000)
        st, d = user.call("GET", "/api/record")
        rec = d.get("trades", [])
        mine = [t for t in rec if t.get("bot_name") == "Record Pub"]
        check("HTTP: /api/record = published bots' closed trades only, "
              "newest first, losers included",
              st == 200 and len(mine) == 2
              and mine[0]["symbol"] == "ETHUSDT" and mine[0]["pnl"] < 0
              and mine[0]["exit_reason"] == "stop_loss"
              and mine[0]["entry_price"] == 50.0
              and mine[0]["exit_price"] == 55.0
              and mine[1]["symbol"] == "BTCUSDT" and mine[1]["pnl"] > 0
              and not any(t.get("bot_name") == "Record Hidden"
                          for t in rec)
              and [t["exit_ts_ms"] for t in rec]
              == sorted((t["exit_ts_ms"] for t in rec), reverse=True),
              f"{st} total={len(rec)} published={len(mine)}")

        # Free-plan quota: a fresh Free user gets 2 searches, then 429.
        free3 = Client(base)
        st, d = free3.call("POST", "/api/signup",
                           {"email": "free3@example.com", "name": "Free3",
                            "password": "secret3"})
        check("third signup is on the Free plan",
              st == 200 and d["user"]["plan"] == "free")
        st, s1 = free3.call("GET", "/api/signal?symbol=BTCUSDT",
                            timeout=300)
        st2, s2 = free3.call("GET", "/api/signal?symbol=ETHUSDT",
                             timeout=300)
        check("HTTP: Free user gets the first 2 signal searches",
              st == 200 and st2 == 200
              and s1.get("quota", {}).get("used") == 1
              and s2.get("quota", {}).get("used") == 2,
              f"{st}/{st2} quota={s2.get('quota')}")
        st, s3 = free3.call("GET", "/api/signal?symbol=SOLUSDT",
                            timeout=300)
        check("HTTP: Free user's 3rd search of the day is a 429 "
              "with quota details",
              st == 429 and s3.get("quota", {}).get("limit") == 2
              and s3.get("quota", {}).get("used") == 2
              and bool(s3.get("error")),
              f"{st} quota={s3.get('quota')}")
        st, ov2 = free3.call("GET", "/api/signals", timeout=300)
        check("HTTP: overview grid is not blocked by the quota",
              st == 200 and isinstance(ov2.get("signals"), list),
              f"{st}")
        st, wp = free3.call("GET", "/api/watchlist")
        check("HTTP: watchlist view is not blocked by the quota",
              st == 200 and wp.get("symbols") == [], f"{st}")
        st, _ = user.call("GET", "/api/signal?symbol=BTCUSDT",
                          timeout=300)
        check("HTTP: Pro user is exempt from the signal quota",
              st == 200, f"{st}")
        ok_admin = True
        for _ in range(3):
            st, _ = admin.call("GET", "/api/signal?symbol=BTCUSDT",
                               timeout=300)
            ok_admin = ok_admin and st == 200
        check("HTTP: admin is exempt from the signal quota", ok_admin)

        # --- hardening endpoints -----------------------------------------
        st, d = admin.call("POST", "/api/admin/backup")
        check("admin can trigger a backup",
              st == 200 and d.get("ok") is True
              and "platform.db" in d.get("files", []),
              ", ".join(d.get("files", [])[:3]))
        st, d = user.call("POST", "/api/admin/backup")
        check("backup creation is admin-only", st == 403,
              f"status={st}")
        req = urllib.request.Request(base + "/api/admin/backup/download")
        with admin.opener.open(req, timeout=60) as resp:
            blob = resp.read()
            ctype = resp.headers.get_content_type()
        check("admin can download the latest backup as a zip",
              resp.status == 200 and ctype == "application/zip"
              and blob[:2] == b"PK", f"{len(blob)} bytes")
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            spa = resp.read().decode("utf-8", "replace")
        check("served SPA carries the Terms + Privacy pages",
              resp.status == 200 and "Terms of Service" in spa
              and "Privacy Policy" in spa)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    unit_tests()
    universe_tests()
    breakout_tests()
    regime_tests()
    range_tests()
    signals_tests()
    tradebise_tests()
    hardening_tests()
    http_tests()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
