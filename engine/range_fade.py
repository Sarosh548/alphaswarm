"""Range Specialist candidate #2 - "Stochastic Range Fade".

Designed ONCE from stated principles and frozen BEFORE any
evaluation (see ~/workspace/alpha-paper-research/range2/DESIGN.md).
This candidate is judged on FORWARD data only: it has never been
backtested, no parameter was fitted to anything, and no second
iteration is permitted by the study protocol.

Why this candidate exists: the RANGING seat is empty. The first
ranging candidate (the project's mean-reversion study) gated
Bollinger + RSI price extremes with a Kaufman efficiency-ratio test
and failed the strict bar: it bought the oversold EXTREME itself,
and at 1h those extremes tended to be capitulations that continued.
This candidate shares no mechanism with it:

  * Entry trigger - a momentum TURN, not an extreme: the slow
    stochastic %K crosses back OUT of its extreme zone (up through
    20 for a long, down through 80 for a short). The trade is taken
    only after the turn has begun, never at the extreme itself.
  * Regime self-gate - three independent sideways tests, ALL of
    which must hold at the signal bar (any undefined measure fails
    the gate closed):
      1. ADX(14) < 20 - Wilder's "no meaningful trend" reading.
      2. |EMA50 now - EMA50 24 bars ago| <= 0.5 x ATR14 - the trend
         line itself is flat in price space over the last day.
      3. The 48-bar Donchian channel is at least 3 x ATR14 wide -
         an established range with room to trade inside it (a
         narrower coil is compression, a different situation).
  * Take profit - the OPPOSITE EDGE of the range being faded (the
    48-bar channel high for a long, the channel low for a short):
    the thesis is range traversal, not reversion to a mean and not
    a fixed R multiple. A proposal is only issued when that target
    is at least 1.5x the stop distance away (a fixed geometry
    coherence rule, so the platform's 1R partial target sits
    inside the path to the final target).
  * Stop loss - entry -/+ 1.5 x ATR14, the project standard
    (config.ATR_SL_MULTIPLIER), unchanged.

Everything else is the unchanged platform machinery: the
deterministic risk policy (1% risk per trade, max 2 open, -3%
daily-loss halt, mandatory SL/TP, stale-data refusal, kill
switch), 0.1%-per-side paper fees, and the platform execution
upgrades (TP1 partial at 1R, break-even stop move, trailing stop)
via engine_range.py + execution.watch_exits.

This module is pure signal logic: it reads candles and returns a
proposal dict (or a block reason). It is NOT part of the Regime
Router's admitted roster - the router keeps sitting out RANGING
until this candidate earns the seat on its forward record.
"""

import config
import indicators
import regime

# --- fixed parameters (DESIGN.md; textbook values, never fitted) ------
STOCH_PERIOD = 14        # Lane's canonical %K lookback
STOCH_SMOOTH = 3         # Lane's slow-stochastic smoothing
STOCH_OVERSOLD = 20.0    # Lane's oversold zone boundary
STOCH_OVERBOUGHT = 80.0  # Lane's overbought zone boundary
ADX_PERIOD = 14          # Wilder canonical
ADX_GATE_MAX = 20.0      # Wilder: below 20, no meaningful trend
EMA_PERIOD = 50          # the project's trend line
EMA_SLOPE_BARS = 24      # one day of hourly bars
EMA_SLOPE_MAX_ATR = 0.5  # trend line may drift at most half an ATR/day
CHANNEL_N = 48           # two days of hourly bars define the range
CHANNEL_MIN_WIDTH_ATR = 3.0  # room for the stop plus a real target
ATR_PERIOD = 14          # project standard
MIN_REWARD_R = 1.5       # target must be >= 1.5x the stop distance


def stochastic_series(candles, period=STOCH_PERIOD, smooth=STOCH_SMOOTH):
    """Slow stochastic %K, aligned with the input (None in warm-up).

    Raw %K = 100 * (close - lowest low) / (highest high - lowest low)
    over the trailing `period` bars, smoothed by an SMA of `smooth`.
    A perfectly flat window (high == low) is defined as 50.0 - the
    mid-zone - so a dead market can never produce a cross."""
    n = len(candles)
    raw = [None] * n
    for i in range(period - 1, n):
        window = candles[i - period + 1:i + 1]
        hh = max(c["high"] for c in window)
        ll = min(c["low"] for c in window)
        if hh == ll:
            raw[i] = 50.0
        else:
            raw[i] = 100.0 * (candles[i]["close"] - ll) / (hh - ll)
    out = [None] * n
    for i in range(n):
        if raw[i] is None:
            continue
        vals = [v for v in raw[max(0, i - smooth + 1):i + 1]
                if v is not None]
        if len(vals) == smooth:
            out[i] = sum(vals) / smooth
    return out


def build_series(candles):
    """Precompute every series the gate/trigger need, once per symbol
    (the smoke test and the tests scan many bars; a live cycle only
    reads the last one)."""
    closes = [c["close"] for c in candles]
    return {
        "adx": regime.adx_series(candles, ADX_PERIOD),
        "ema": indicators.ema_series(closes, EMA_PERIOD),
        "stoch": stochastic_series(candles),
    }


def channel(candles, i, n=CHANNEL_N):
    """(channel_high, channel_low) of the n bars ending at bar i
    (bars i-n+1 .. i): the established range up to and including the
    signal bar."""
    window = candles[i - n + 1:i + 1]
    return (max(c["high"] for c in window),
            min(c["low"] for c in window))


def range_measures(candles, i, atr_values, series=None):
    """Every gate/trigger measure at bar i, plus the gate verdict.

    Returns a dict: atr, adx, slope_atr (EMA drift in ATR units),
    width_atr (channel width in ATR units), channel_high,
    channel_low, stoch_k, stoch_k_prev, gate_open (bool),
    gate_reason ("ok", or which gate test failed / "warmup" /
    "no_atr"). Measures that cannot be computed are None and the
    gate fails closed."""
    if series is None:
        series = build_series(candles)
    m = {"atr": None, "adx": None, "slope_atr": None,
         "width_atr": None, "channel_high": None, "channel_low": None,
         "stoch_k": None, "stoch_k_prev": None,
         "gate_open": False, "gate_reason": "warmup"}
    if i < CHANNEL_N - 1 or i < 1:
        return m
    atr = atr_values[i]
    if atr is None or atr <= 0:
        m["gate_reason"] = "no_atr"
        return m
    m["atr"] = atr
    adx = series["adx"][i]
    ema = series["ema"]
    stoch = series["stoch"]
    if (adx is None or ema[i] is None or i < EMA_SLOPE_BARS
            or ema[i - EMA_SLOPE_BARS] is None
            or stoch[i] is None or stoch[i - 1] is None):
        return m
    hi, lo = channel(candles, i)
    m["adx"] = adx
    m["slope_atr"] = abs(ema[i] - ema[i - EMA_SLOPE_BARS]) / atr
    m["width_atr"] = (hi - lo) / atr
    m["channel_high"] = hi
    m["channel_low"] = lo
    m["stoch_k"] = stoch[i]
    m["stoch_k_prev"] = stoch[i - 1]
    if adx >= ADX_GATE_MAX:
        m["gate_reason"] = "gate_trend"
    elif m["slope_atr"] > EMA_SLOPE_MAX_ATR:
        m["gate_reason"] = "gate_slope"
    elif m["width_atr"] < CHANNEL_MIN_WIDTH_ATR:
        m["gate_reason"] = "gate_narrow"
    else:
        m["gate_open"] = True
        m["gate_reason"] = "ok"
    return m


def range_signal(candles, i, atr_values, series=None):
    """Evaluate the range-fade rule on closed bar i.

    Returns (proposal, reason):
      proposal - dict {direction, entry, stop_loss, take_profit,
                 channel_high, channel_low, atr, stoch_k,
                 stoch_k_prev, adx, slope_atr, width_atr} or None
      reason   - "long"/"short" when a proposal is returned, else
                 one of "warmup", "no_atr", "gate_trend",
                 "gate_slope", "gate_narrow" (the self-gate refused:
                 the market is not demonstrably ranging), "no_cross"
                 (gate open, but %K has not turned out of its zone),
                 "target_too_close" (gate open and a cross fired,
                 but the far side of the range is < 1.5R away).
    The gate is checked first and at the signal bar only: a cross
    that fires while the gate is closed is consumed, never retried.
    """
    m = range_measures(candles, i, atr_values, series)
    if not m["gate_open"]:
        return None, m["gate_reason"]
    k_prev, k_now = m["stoch_k_prev"], m["stoch_k"]
    direction = None
    if k_prev <= STOCH_OVERSOLD < k_now:
        direction = "long"
    elif k_prev >= STOCH_OVERBOUGHT > k_now:
        direction = "short"
    if direction is None:
        return None, "no_cross"
    close = candles[i]["close"]
    dist = config.ATR_SL_MULTIPLIER * m["atr"]
    if direction == "long":
        stop, target = close - dist, m["channel_high"]
    else:
        stop, target = close + dist, m["channel_low"]
    if abs(target - close) < MIN_REWARD_R * dist:
        return None, "target_too_close"
    return {
        "direction": direction, "entry": round(close, 4),
        "stop_loss": round(stop, 4), "take_profit": round(target, 4),
        "channel_high": m["channel_high"],
        "channel_low": m["channel_low"],
        "atr": m["atr"], "stoch_k": round(k_now, 1),
        "stoch_k_prev": round(k_prev, 1), "adx": round(m["adx"], 1),
        "slope_atr": round(m["slope_atr"], 3),
        "width_atr": round(m["width_atr"], 2),
    }, direction
