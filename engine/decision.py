"""Decision step: weighted consensus of the analyst votes.

A trade proposal exists only when at least MIN_AGENT_AGREEMENT agents agree
on one direction AND their average confidence is at least
MIN_AVG_CONFIDENCE. Every proposal carries an entry zone, a stop loss and
a take profit derived from ATR and nearby support/resistance; a proposal
without SL/TP is invalid by construction (build_proposal returns None).
"""

import config


def build_proposal(symbol, snapshot, votes):
    longs = [v for v in votes if v.direction == "long"]
    shorts = [v for v in votes if v.direction == "short"]
    side, agreeing = None, []
    if len(longs) >= len(shorts):
        side, agreeing = "long", longs
    else:
        side, agreeing = "short", shorts
    if len(agreeing) < config.MIN_AGENT_AGREEMENT:
        return None
    avg_conf = sum(v.confidence for v in agreeing) / len(agreeing)
    if avg_conf < config.MIN_AVG_CONFIDENCE:
        return None

    price = snapshot["price"]
    atr = snapshot["atr14"]
    if not atr or atr <= 0:
        return None
    entry = price
    entry_zone = (entry - 0.25 * atr, entry + 0.25 * atr)

    if side == "long":
        sr_stop = snapshot["support"] * 0.998 if snapshot["support"] else None
        atr_stop = entry - config.ATR_SL_MULTIPLIER * atr
        # Use the nearer (less risky) valid stop below entry.
        stop = max(sr_stop, atr_stop) if sr_stop and sr_stop < entry else atr_stop
        if stop >= entry:
            return None
        risk = entry - stop
        take_profit = entry + config.TP_R_MULTIPLE * risk
    else:
        sr_stop = snapshot["resistance"] * 1.002 if snapshot["resistance"] else None
        atr_stop = entry + config.ATR_SL_MULTIPLIER * atr
        stop = min(sr_stop, atr_stop) if sr_stop and sr_stop > entry else atr_stop
        if stop <= entry:
            return None
        risk = stop - entry
        take_profit = entry - config.TP_R_MULTIPLE * risk

    return {
        "symbol": symbol,
        "direction": side,
        "entry": round(entry, 4),
        "entry_zone": (round(entry_zone[0], 4), round(entry_zone[1], 4)),
        "stop_loss": round(stop, 4),
        "take_profit": round(take_profit, 4),
        "avg_confidence": round(avg_conf, 3),
        "agreeing_agents": [v.agent for v in agreeing],
        "votes": [v.to_dict() for v in votes],
    }
