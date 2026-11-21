"""Higher-timeframe (4h) trend labels for the breakout runner.

The HTFTrend class below is copied VERBATIM from phase2_sim.py in the
parent project (only its container module differs), so the trend labels
this live runner sees are computed exactly as in the Phase 2 / breakout
backtests: EMA50 vs EMA200 on 4h closes, using only 4h candles that
have fully CLOSED by the decision time - no look-ahead.
"""

import bisect

import indicators

FOURH_MS = 14_400_000


class HTFTrend:
    """4h trend label (EMA50 vs EMA200 on 4h closes) as known at any
    decision time. Only 4h candles that have fully CLOSED by the decision
    time are used - no look-ahead."""

    def __init__(self, htf_candles):
        self.candles = htf_candles
        closes = [c["close"] for c in htf_candles]
        self.ema50 = indicators.ema_series(closes, 50)
        self.ema200 = indicators.ema_series(closes, 200)
        # close time of each 4h candle
        self.close_times = [c["ts"] + FOURH_MS for c in htf_candles]

    def at(self, decision_ms):
        idx = bisect.bisect_right(self.close_times, decision_ms) - 1
        if idx < 0:
            return "unknown"
        e50, e200 = self.ema50[idx], self.ema200[idx]
        if e50 is None or e200 is None:
            return "unknown"
        return "up" if e50 > e200 else "down"
