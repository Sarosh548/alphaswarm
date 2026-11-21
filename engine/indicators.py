"""Technical indicators in pure Python (lists in, lists/values out).

All series functions return a list aligned with the input, using None where
the indicator is not yet defined (warm-up period).
"""


def ema_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma_series(values, period):
    out = [None] * len(values)
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= period:
            run -= values[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def rsi_series(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def macd_series(closes, fast=12, slow=26, signal=9):
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    line = [None if (f is None or s is None) else f - s
            for f, s in zip(ema_fast, ema_slow)]
    defined = [v for v in line if v is not None]
    sig_defined = ema_series(defined, signal) if len(defined) >= signal else []
    signal_line = [None] * len(line)
    offset = len(line) - len(defined)
    for j, v in enumerate(sig_defined):
        signal_line[offset + j] = v
    hist = [None if (l is None or s is None) else l - s
            for l, s in zip(line, signal_line)]
    return line, signal_line, hist


def bollinger_series(closes, period=20, mult=2.0):
    mid = sma_series(closes, period)
    upper, lower = [None] * len(closes), [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mean = mid[i]
        var = sum((x - mean) ** 2 for x in window) / period
        sd = var ** 0.5
        upper[i] = mean + mult * sd
        lower[i] = mean - mult * sd
    return mid, upper, lower


def atr_series(candles, period=14):
    out = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["high"] - c["low"])
        else:
            prev_close = candles[i - 1]["close"]
            trs.append(max(c["high"] - c["low"],
                           abs(c["high"] - prev_close),
                           abs(c["low"] - prev_close)))
    atr = sum(trs[1:period + 1]) / period
    out[period] = atr
    for i in range(period + 1, len(candles)):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def support_resistance(candles, lookback=50):
    """Recent support = lowest low, resistance = highest high of the window
    (excluding the last candle so levels are 'already established')."""
    window = candles[-lookback - 1:-1] or candles[:-1]
    if not window:
        return None, None
    return (min(c["low"] for c in window), max(c["high"] for c in window))


def volume_change(candles, lookback=20):
    """Latest volume vs the average of the prior `lookback` candles, in %."""
    if len(candles) <= lookback:
        return 0.0
    avg = sum(c["volume"] for c in candles[-lookback - 1:-1]) / lookback
    if avg == 0:
        return 0.0
    return (candles[-1]["volume"] - avg) / avg * 100.0


def snapshot_from_series(candles, idx, series):
    """Build an indicator snapshot dict at candle index `idx` from
    precomputed series (used by the backtest for speed)."""
    closes = [c["close"] for c in candles]
    window = candles[max(0, idx - 50):idx]
    support = min((c["low"] for c in window), default=None)
    resistance = max((c["high"] for c in window), default=None)
    vol_avg = (sum(c["volume"] for c in candles[max(0, idx - 20):idx]) / 20.0
               if idx >= 20 else 0.0)
    vol_chg = ((candles[idx]["volume"] - vol_avg) / vol_avg * 100.0
               if vol_avg else 0.0)
    return {
        "price": closes[idx],
        "ema20": series["ema20"][idx],
        "ema50": series["ema50"][idx],
        "ema200": series["ema200"][idx],
        "rsi14": series["rsi14"][idx],
        "macd_hist": series["macd_hist"][idx],
        "bb_mid": series["bb_mid"][idx],
        "bb_upper": series["bb_upper"][idx],
        "bb_lower": series["bb_lower"][idx],
        "atr14": series["atr14"][idx],
        "support": support,
        "resistance": resistance,
        "volume_change_pct": vol_chg,
    }


def compute_series(candles):
    closes = [c["close"] for c in candles]
    _, _, macd_hist = macd_series(closes)
    bb_mid, bb_upper, bb_lower = bollinger_series(closes)
    return {
        "ema20": ema_series(closes, 20),
        "ema50": ema_series(closes, 50),
        "ema200": ema_series(closes, 200),
        "rsi14": rsi_series(closes, 14),
        "macd_hist": macd_hist,
        "bb_mid": bb_mid,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "atr14": atr_series(candles, 14),
    }


def compute_snapshot(candles, htf_candles=None):
    """Snapshot of the latest closed candle. Adds a higher-timeframe trend
    label when 4h candles are supplied."""
    series = compute_series(candles)
    snap = snapshot_from_series(candles, len(candles) - 1, series)
    snap["last_ts"] = candles[-1]["ts"]
    if htf_candles:
        htf_closes = [c["close"] for c in htf_candles]
        e50 = ema_series(htf_closes, 50)
        e200 = ema_series(htf_closes, 200)
        if e50[-1] is not None and e200[-1] is not None:
            snap["htf_trend"] = "up" if e50[-1] > e200[-1] else "down"
        else:
            snap["htf_trend"] = "unknown"
    else:
        snap["htf_trend"] = "unknown"
    return snap
