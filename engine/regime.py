"""Market-regime classifier: which SITUATION is this symbol in?

The Regime Router (engine_router.py) asks this module one question per
symbol per cycle - "what kind of market is this right now?" - and then
hands the entry decision to the specialist strategy that has PROVEN
itself in that situation, or sits out when no specialist has. This
module is a classifier, not a strategy: it never proposes a trade, it
only labels the situation.

Measures (all causal - computed from closed candles only, evaluated
at the last bar unless an index is given):

  * Kaufman Efficiency Ratio, ER(20): |net price change over 20 bars|
    divided by the total path travelled. 1.0 = price went in a
    straight line; near 0 = price churned and got nowhere. The
    project's Phase 2 diagnosis found v1's entries performed best in
    the ER >= 0.50 bucket and worst in mixed/sideways conditions, and
    the project's mean-reversion research used ER <= 0.25 as its
    ranging gate - both boundaries are reused here unchanged.
  * ADX(14), Wilder's Average Directional Index: trend STRENGTH
    regardless of direction. Wilder's classic readings are used:
    >= 25 = a real trend, < 20 = no meaningful trend. Note ADX does
    not know direction and rises on any sustained one-way move,
    including one leg of an oscillation - which is why the trend
    labels below ALSO require ER agreement (an efficient path), so a
    direction flip inside the window cannot pass as a "clean trend".
  * Bollinger Bandwidth percentile: bandwidth = (upper - lower) / mid
    of the standard 20-bar, 2-sigma bands, ranked (mid-rank) against
    its own trailing 120 values. Low percentile = volatility has
    contracted (a squeeze, in the TTM-Squeeze sense of "lowest
    bandwidth in its recent history"); high percentile = volatility
    is expanding.

Thresholds (FIXED - chosen from the standard literature values and
the project's own earlier diagnosis boundaries, documented above;
they were NOT fitted or tuned against any result, and this module's
job is description, not profit):

  TREND_EXPANSION  ADX >= 25 AND ER >= 0.50 AND bandwidth pct >= 0.50
                   (strong + efficient + volatility expanding)
  CLEAN_TREND      ADX >= 25 AND ER >= 0.30 (and not expansion)
                   (an established trend, less explosive)
  COMPRESSION      bandwidth pct <= 0.20 (lowest quintile) and not a
                   trend by the rules above
  RANGING          ER <= 0.25 AND ADX < 20 (sideways, no trend)
  MIXED_UNCLEAR    everything else, including "not enough history"

Precedence is top-down as listed: trend labels are checked before
compression because a trending market's bandwidth is rarely in its
lowest quintile, and when the two genuinely conflict the trend is the
tradeable fact; compression is checked before ranging because a
contracted-volatility coil is the more specific signature (both sit
out today - neither has a proven specialist yet).

Honest limitation: a label is a routing hint, not an order. Each
specialist still applies its own entry rules and the platform risk
policy still gates every trade. Whether this routing actually helps
can only be proven by the forward paper record, which starts when
the first Regime Router bot runs.
"""

import indicators

# --- fixed thresholds (see module docstring; do not tune) -------------
ER_PERIOD = 20
ADX_PERIOD = 14
BBW_WINDOW = 120          # trailing bandwidth values for the rank
BBW_MIN_POINTS = 50       # fail safe below this many defined values

ER_EXPANSION = 0.50       # Phase 2 diagnosis bucket boundary
ER_TREND_MIN = 0.30       # a "clean" trend must still travel efficiently
ER_RANGE_MAX = 0.25       # the project's mean-reversion ranging gate
ADX_TREND = 25.0          # Wilder: a real trend
ADX_RANGE_MAX = 20.0      # Wilder: no meaningful trend below this
BBW_COMPRESSION_PCT = 0.20  # lowest quintile = squeeze
BBW_EXPANSION_PCT = 0.50    # above-median bandwidth = expanding

LABELS = ("TREND_EXPANSION", "CLEAN_TREND", "COMPRESSION", "RANGING",
          "MIXED_UNCLEAR")

PLAIN = {
    "TREND_EXPANSION": (
        "a strong directional move is underway - price is travelling "
        "efficiently in one direction and volatility is expanding"),
    "CLEAN_TREND": (
        "an established trend is in place - price is moving in one "
        "direction with real strength behind it"),
    "COMPRESSION": (
        "volatility has contracted to its lowest levels in months of "
        "hourly bars - the market is coiling, and a coil by itself is "
        "not a trade"),
    "RANGING": (
        "price is moving sideways - it travels a lot and gets "
        "nowhere, with no trend strength behind it"),
    "MIXED_UNCLEAR": (
        "the measures disagree - there is no clear market situation "
        "to match a specialist to"),
}


def adx_series(candles, period=ADX_PERIOD):
    """Wilder's ADX, aligned with the input (None during warm-up).
    First defined value at index 2*period - 1."""
    n = len(candles)
    out = [None] * n
    if n <= 2 * period:
        return out
    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr[i] = max(high - low, abs(high - prev_close),
                    abs(low - prev_close))
        up = high - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - low
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    def dx_of(s_plus, s_minus, s_tr):
        if s_tr == 0:
            return 0.0
        pdi = 100.0 * s_plus / s_tr
        mdi = 100.0 * s_minus / s_tr
        total = pdi + mdi
        return 0.0 if total == 0 else 100.0 * abs(pdi - mdi) / total

    s_tr = sum(tr[1:period + 1])
    s_plus = sum(plus_dm[1:period + 1])
    s_minus = sum(minus_dm[1:period + 1])
    dx = [None] * n
    dx[period] = dx_of(s_plus, s_minus, s_tr)
    for i in range(period + 1, n):
        s_tr = s_tr - s_tr / period + tr[i]
        s_plus = s_plus - s_plus / period + plus_dm[i]
        s_minus = s_minus - s_minus / period + minus_dm[i]
        dx[i] = dx_of(s_plus, s_minus, s_tr)
    first = 2 * period - 1
    if first >= n:
        return out
    adx = sum(dx[period:first + 1]) / period
    out[first] = adx
    for i in range(first + 1, n):
        adx = (adx * (period - 1) + dx[i]) / period
        out[i] = adx
    return out


def efficiency_ratio(closes, i, period=ER_PERIOD):
    """Kaufman ER at index i: |net change| / total path over the last
    `period` bars. None before warm-up; 0.0 for a perfectly flat path."""
    if i < period:
        return None
    net = abs(closes[i] - closes[i - period])
    path = sum(abs(closes[j] - closes[j - 1])
               for j in range(i - period + 1, i + 1))
    if path == 0:
        return 0.0
    return net / path


def _percentile_midrank(values, i, window, min_points):
    """Mid-rank percentile of values[i] within the trailing `window`
    defined values ending at i (causal). Mid-rank means a constant
    series ranks 0.50, not 0. None when the current value is undefined
    or too few defined values exist."""
    cur = values[i]
    if cur is None:
        return None
    vals = [v for v in values[max(0, i - window + 1):i + 1]
            if v is not None]
    if len(vals) < min_points:
        return None
    below = sum(1 for v in vals if v < cur)
    equal = sum(1 for v in vals if v == cur)
    return (below + 0.5 * equal) / len(vals)


def classify_regime(candles, idx=None):
    """Classify the market situation at candle `idx` (default: the
    last candle). Returns a dict:
      label       one of LABELS
      er          Kaufman efficiency ratio (or None)
      adx         Wilder ADX(14) (or None)
      bbw_pct     Bollinger bandwidth percentile 0..1 (or None)
      plain       one plain-language sentence describing the label
      measures    compact machine-readable detail for the journal
    Insufficient history yields MIXED_UNCLEAR - the router sits out,
    which is the safe behaviour on unknown ground."""
    if idx is None:
        idx = len(candles) - 1
    result = {"label": "MIXED_UNCLEAR", "er": None, "adx": None,
              "bbw_pct": None, "plain": PLAIN["MIXED_UNCLEAR"],
              "measures": "insufficient history"}
    if idx < 0 or not candles:
        return result
    closes = [c["close"] for c in candles]
    er = efficiency_ratio(closes, idx)
    adx = adx_series(candles)[idx]
    mid, upper, lower = indicators.bollinger_series(closes)
    bbw = [None if (u is None or l is None or m in (None, 0))
           else (u - l) / m
           for u, l, m in zip(upper, lower, mid)]
    pct = _percentile_midrank(bbw, idx, BBW_WINDOW, BBW_MIN_POINTS)
    result["er"] = None if er is None else round(er, 3)
    result["adx"] = None if adx is None else round(adx, 1)
    result["bbw_pct"] = None if pct is None else round(pct, 3)
    if er is None or adx is None or pct is None:
        return result
    result["measures"] = (f"er={er:.3f} adx={adx:.1f} "
                          f"bbw_pct={pct:.3f}")
    if adx >= ADX_TREND and er >= ER_EXPANSION and pct >= BBW_EXPANSION_PCT:
        label = "TREND_EXPANSION"
    elif adx >= ADX_TREND and er >= ER_TREND_MIN:
        label = "CLEAN_TREND"
    elif pct <= BBW_COMPRESSION_PCT:
        label = "COMPRESSION"
    elif er <= ER_RANGE_MAX and adx < ADX_RANGE_MAX:
        label = "RANGING"
    else:
        label = "MIXED_UNCLEAR"
    result["label"] = label
    result["plain"] = PLAIN[label]
    return result
