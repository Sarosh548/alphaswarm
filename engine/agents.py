"""Analyst agents: deterministic, rule-based, no LLM, no API keys.

Each agent reads the same indicator snapshot and returns a Vote:
direction in {"long", "short", "none"}, a confidence in [0, 1], and
evidence strings quoting the actual indicator values behind the vote.
"""

from dataclasses import dataclass, field


@dataclass
class Vote:
    agent: str
    direction: str  # "long" | "short" | "none"
    confidence: float
    evidence: list = field(default_factory=list)

    def to_dict(self):
        return {"agent": self.agent, "direction": self.direction,
                "confidence": round(self.confidence, 3),
                "evidence": self.evidence}


def _clamp(x):
    return max(0.0, min(1.0, x))


class TrendAgent:
    """Follows the EMA stack and the higher-timeframe trend."""
    name = "TrendAgent"

    def vote(self, s):
        ev = []
        if None in (s["ema20"], s["ema50"], s["ema200"]):
            return Vote(self.name, "none", 0.0, ["EMA warm-up incomplete"])
        price, e20, e50, e200 = s["price"], s["ema20"], s["ema50"], s["ema200"]
        ev.append(f"price={price:.2f} EMA20={e20:.2f} EMA50={e50:.2f} "
                  f"EMA200={e200:.2f} HTF(4h)={s.get('htf_trend', 'unknown')}")
        score = 0.0
        if price > e20 > e50 > e200:
            score = 1.0
        elif price < e20 < e50 < e200:
            score = -1.0
        elif price > e200 and e50 > e200:
            score = 0.6
        elif price < e200 and e50 < e200:
            score = -0.6
        elif price > e50:
            score = 0.3
        elif price < e50:
            score = -0.3
        htf = s.get("htf_trend", "unknown")
        if htf == "up" and score < 0:
            score *= 0.5
            ev.append("HTF trend is up: short conviction halved")
        if htf == "down" and score > 0:
            score *= 0.5
            ev.append("HTF trend is down: long conviction halved")
        if score > 0.25:
            return Vote(self.name, "long", _clamp(0.4 + 0.6 * score), ev)
        if score < -0.25:
            return Vote(self.name, "short", _clamp(0.4 + 0.6 * abs(score)), ev)
        return Vote(self.name, "none", _clamp(abs(score)), ev)


class MomentumAgent:
    """Reads RSI(14) and the MACD histogram."""
    name = "MomentumAgent"

    def vote(self, s):
        ev = []
        rsi, hist = s["rsi14"], s["macd_hist"]
        if rsi is None or hist is None:
            return Vote(self.name, "none", 0.0, ["RSI/MACD warm-up incomplete"])
        ev.append(f"RSI14={rsi:.1f} MACD_hist={hist:.4f}")
        score = 0.0
        if rsi >= 55:
            score += min((rsi - 55) / 30.0, 1.0) * 0.6
        elif rsi <= 45:
            score -= min((45 - rsi) / 30.0, 1.0) * 0.6
        if hist > 0:
            score += 0.4
        elif hist < 0:
            score -= 0.4
        if rsi >= 78:  # overbought exhaustion: don't chase longs
            score -= 0.5
            ev.append("RSI >= 78: overbought, long momentum discounted")
        if rsi <= 22:  # oversold exhaustion: don't chase shorts
            score += 0.5
            ev.append("RSI <= 22: oversold, short momentum discounted")
        if score > 0.3:
            return Vote(self.name, "long", _clamp(0.35 + 0.65 * score), ev)
        if score < -0.3:
            return Vote(self.name, "short", _clamp(0.35 + 0.65 * abs(score)), ev)
        return Vote(self.name, "none", _clamp(abs(score)), ev)


class MeanReversionAgent:
    """Fades extremes: price stretched far from the Bollinger mid-band or
    pinned to a band edge with an extreme RSI."""
    name = "MeanReversionAgent"

    def vote(self, s):
        ev = []
        price, mid, up, low, rsi = (s["price"], s["bb_mid"], s["bb_upper"],
                                    s["bb_lower"], s["rsi14"])
        if None in (mid, up, low, rsi) or up == low:
            return Vote(self.name, "none", 0.0, ["Bollinger warm-up incomplete"])
        pct_b = (price - low) / (up - low)
        ev.append(f"price={price:.2f} BB_mid={mid:.2f} BB_upper={up:.2f} "
                  f"BB_lower={low:.2f} %B={pct_b:.2f} RSI14={rsi:.1f}")
        if pct_b <= 0.05 and rsi <= 32:
            return Vote(self.name, "long", 0.75,
                        ev + ["at lower band with oversold RSI: bounce setup"])
        if pct_b >= 0.95 and rsi >= 68:
            return Vote(self.name, "short", 0.75,
                        ev + ["at upper band with overbought RSI: fade setup"])
        if pct_b <= 0.15 and rsi <= 40:
            return Vote(self.name, "long", 0.55,
                        ev + ["near lower band, RSI soft: mild long bias"])
        if pct_b >= 0.85 and rsi >= 60:
            return Vote(self.name, "short", 0.55,
                        ev + ["near upper band, RSI firm: mild short bias"])
        return Vote(self.name, "none", 0.2,
                    ev + ["price mid-range: no reversion setup"])


class VolatilityAgent:
    """Votes with the trend only when volatility is tradable: ATR expanding
    but not chaotic, and price breaking toward recent support/resistance.
    Abstains in dead or explosive conditions."""
    name = "VolatilityAgent"

    def vote(self, s):
        ev = []
        price, atr, sup, res = (s["price"], s["atr14"], s["support"],
                                s["resistance"])
        if None in (atr, sup, res) or price == 0:
            return Vote(self.name, "none", 0.0, ["ATR/S-R warm-up incomplete"])
        atr_pct = atr / price * 100.0
        ev.append(f"ATR14={atr:.2f} ({atr_pct:.2f}% of price) support={sup:.2f} "
                  f"resistance={res:.2f} volume_change={s['volume_change_pct']:.1f}%")
        if atr_pct < 0.15:
            return Vote(self.name, "none", 0.3,
                        ev + ["volatility too low: moves not worth the risk"])
        if atr_pct > 4.0:
            return Vote(self.name, "none", 0.3,
                        ev + ["volatility extreme: standing aside"])
        dist_res = (res - price) / atr if atr else 999
        dist_sup = (price - sup) / atr if atr else 999
        ev.append(f"distance: resistance={dist_res:.1f} ATR, support={dist_sup:.1f} ATR")
        if dist_res <= 0.5 and s["volume_change_pct"] > -20:
            return Vote(self.name, "long", 0.6,
                        ev + ["pressing against resistance with live volume"])
        if dist_sup <= 0.5 and s["volume_change_pct"] > -20:
            return Vote(self.name, "short", 0.6,
                        ev + ["pressing against support with live volume"])
        if dist_res <= 0.5:
            return Vote(self.name, "short", 0.5,
                        ev + ["at resistance but volume fading: rejection bias"])
        if dist_sup <= 0.5:
            return Vote(self.name, "long", 0.5,
                        ev + ["at support but volume fading: bounce bias"])
        return Vote(self.name, "none", 0.3,
                    ev + ["mid-range: no volatility edge"])


AGENTS = [TrendAgent(), MomentumAgent(), MeanReversionAgent(), VolatilityAgent()]


def run_agents(snapshot):
    return [a.vote(snapshot) for a in AGENTS]
