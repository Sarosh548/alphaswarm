"""Breakout strategy candidate — designed ONCE from stated principles,
evaluated ONCE (walk-forward, see RESULTS_BREAKOUT.md). No parameter
was tuned against the evaluation data, and no second iteration is
permitted by the study protocol.

Why this candidate exists: the Phase 2 diagnosis of v1 found its
entries performed best in trending conditions (Kaufman efficiency
ratio >= 0.50 bucket: profit factor 1.47, +3,040.60) and worst in
mixed/sideways conditions. A volatility-breakout rule is the
principled next candidate: it only acts when price leaves a range,
which is the moment a trend can begin.

DESIGN (fixed before evaluation)
--------------------------------
Entry — Donchian-channel breakout, 1h candles, evaluated on closed
bars only:
  * Channel at bar i = highest high / lowest low of the 20 bars
    BEFORE bar i (bars i-20 .. i-1).
  * A breakout is an EVENT, not a state: a long candidate fires on
    the first close above the channel high whose previous close was
    still inside (at or below) its own channel high; a short
    candidate mirrors below the channel low. Closes that merely
    remain outside the channel are continuation, not new breakouts
    (the classic Turtle-rule reading of a Donchian system).

Trend context — the same fail-closed rule as strategy v2:
  * The 4h trend (EMA50 vs EMA200 on 4h candles fully closed before
    the decision time, via phase2_sim.HTFTrend) must agree: long
    only when "up", short only when "down". "unknown" blocks.

Volatility guard — no entries into exhausted volatility:
  * The current ATR14's percentile rank within its own trailing 200
    ATR14 values (causal, defined values only) must be < 0.90.
    A breakout printed when volatility is already in its top decile
    is treated as exhaustion, not opportunity, and skipped.
  * Fewer than 50 defined ATR values in the window blocks the
    signal (fail closed).

Risk / exits — the SAME deterministic policy as the existing engine:
  * Stop distance = 1.5 x ATR14 at the signal bar
    (config.ATR_SL_MULTIPLIER); take profit = 2R
    (config.TP_R_MULTIPLE). SL/TP are placed around the reference
    entry (signal-bar close) and shifted to the actual fill (next
    bar's open) exactly as the engine's walk does.
  * Position size, 1% risk cap, max 2 open positions and the -3%
    daily-loss halt all come from policy.check_proposal, unchanged.
  * Simulation charges a 0.1% fee per side on notional (entry and
    exit) — note the Phase 2 v1/v2 numbers this is compared against
    were simulated WITHOUT fees, so the comparison is conservative
    for breakout, and that is disclosed in the results.

This module is pure signal logic: it reads candles and returns a
proposal dict (or a block reason). In the original study the walk
lived in breakout_sim.py; in this platform copy (copied verbatim from
the proven breakout study / forward runner) the live walk lives in
engine_breakout.py, and the platform's execution upgrades apply on
top (see RESULTS_HARDENING.md for the stated compromise).
"""

import config

DONCHIAN_N = 20          # channel lookback, bars
ATR_PERIOD = 14          # matches the engine's ATR14
ATR_PCT_WINDOW = 200     # trailing ATR values for the percentile rank
ATR_PCT_MAX = 0.90       # top-decile volatility blocks entry
ATR_PCT_MIN_POINTS = 50  # fail closed below this many defined values


def donchian(candles, i, n=DONCHIAN_N):
    """(channel_high, channel_low) from the n bars strictly before i."""
    window = candles[i - n:i]
    return (max(c["high"] for c in window),
            min(c["low"] for c in window))


def atr_percentile(atr_values, i, window=ATR_PCT_WINDOW):
    """Percentile rank of atr_values[i] within the trailing `window`
    ATR values ending at i (defined values only, causal). Returns
    None when the current value is undefined or the window holds
    fewer than ATR_PCT_MIN_POINTS defined values."""
    cur = atr_values[i]
    if cur is None:
        return None
    vals = [v for v in atr_values[max(0, i - window + 1):i + 1]
            if v is not None]
    if len(vals) < ATR_PCT_MIN_POINTS:
        return None
    return sum(1 for v in vals if v < cur) / len(vals)


def breakout_signal(candles, i, atr_values, htf_trend):
    """Evaluate the breakout rule on closed bar i.

    Returns (proposal, reason):
      proposal - dict {direction, entry, stop_loss, take_profit,
                 channel_high, channel_low, atr, atr_pct} or None
      reason   - "long"/"short" when a proposal is returned, else one
                 of "inside" (no fresh cross), "trend_block",
                 "vol_guard", "no_atr".
    """
    if i < DONCHIAN_N + 1:
        return None, "inside"
    hi, lo = donchian(candles, i)
    hi_prev, lo_prev = donchian(candles, i - 1)
    close, prev_close = candles[i]["close"], candles[i - 1]["close"]

    direction = None
    if close > hi and prev_close <= hi_prev:
        direction = "long"
    elif close < lo and prev_close >= lo_prev:
        direction = "short"
    if direction is None:
        return None, "inside"

    # Trend context: fail closed, exactly as strategy v2's Change A.
    if htf_trend not in ("up", "down"):
        return None, "trend_block"
    if (htf_trend == "up") != (direction == "long"):
        return None, "trend_block"

    # Volatility guard.
    atr = atr_values[i]
    if atr is None or atr <= 0:
        return None, "no_atr"
    pct = atr_percentile(atr_values, i)
    if pct is None:
        return None, "no_atr"
    if pct >= ATR_PCT_MAX:
        return None, "vol_guard"

    dist = config.ATR_SL_MULTIPLIER * atr
    if direction == "long":
        stop, target = close - dist, close + config.TP_R_MULTIPLE * dist
    else:
        stop, target = close + dist, close - config.TP_R_MULTIPLE * dist
    return {
        "direction": direction, "entry": round(close, 4),
        "stop_loss": round(stop, 4), "take_profit": round(target, 4),
        "channel_high": hi, "channel_low": lo,
        "atr": atr, "atr_pct": round(pct, 3),
    }, direction
