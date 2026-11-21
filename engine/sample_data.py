"""Bundled deterministic sample candles.

Used ONLY as an offline fallback when the Binance public API cannot be
reached, and for tests. The series are synthetic (seeded random walk with
trend regimes) - they are not real market history and are labelled as
"source: sample" everywhere they are used.
"""

import math
import random
import time

_BASE = {"BTCUSDT": 64000.0, "ETHUSDT": 3300.0, "SOLUSDT": 200.0}
_INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000}


def sample_candles(symbol: str, interval: str, limit: int = 400):
    """Return `limit` synthetic candles ending at the current interval."""
    seed = hash((symbol, interval)) & 0xFFFFFFFF
    rng = random.Random(seed)
    step_ms = _INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000)
    end_ms = now_ms - (now_ms % step_ms)  # start of current (incomplete) candle
    start_ms = end_ms - limit * step_ms

    price = _BASE.get(symbol, 100.0)
    candles = []
    for i in range(limit):
        # Regime-switching drift: 120-candle blocks alternate trend direction.
        regime = (i // 120) % 3
        drift = {0: 0.0012, 1: -0.0011, 2: 0.0}[regime]
        wave = 0.0015 * math.sin(i / 18.0)
        shock = rng.gauss(0, 0.006)
        open_p = price
        close_p = max(open_p * (1 + drift + wave + shock), open_p * 0.5)
        high_p = max(open_p, close_p) * (1 + abs(rng.gauss(0, 0.002)))
        low_p = min(open_p, close_p) * (1 - abs(rng.gauss(0, 0.002)))
        volume = 1000.0 * (1 + 0.3 * math.sin(i / 25.0)) * rng.uniform(0.7, 1.3)
        candles.append({
            "ts": start_ms + i * step_ms,
            "open": round(open_p, 4),
            "high": round(high_p, 4),
            "low": round(low_p, 4),
            "close": round(close_p, 4),
            "volume": round(volume, 3),
        })
        price = close_p
    return candles
