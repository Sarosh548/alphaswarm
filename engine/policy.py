"""Risk policy: a deterministic gate between a proposal and the simulator.

Every rule here is a plain yes/no check with a written reason. Nothing is
probabilistic and nothing can be argued with: if any check fails, the
proposal is rejected and the reasons are journaled.

Rules (original to this project):
  P1  paper mode only (config.MODE must be "paper")
  P2  kill switch: HALT file present or ALPHAPAPER_HALT env set -> reject all
  P3  stop loss and take profit must both be present and on the correct side
  P4  risk per trade (qty x |entry - stop|) must be <= 1% of equity;
      position size is computed here so the rule holds by construction
  P5  open positions must be < 2 across all symbols
  P6  daily loss: if today's realised PnL <= -3% of day-start equity,
      trading is halted for the rest of the day
  P7  market data must be fresh (last candle no older than 2 intervals)
  P8  notional size is capped at the fixed 3x simulated leverage
"""

import os
import time

import config


def kill_switch_active(base_dir="."):
    if os.environ.get(config.HALT_ENV, "").lower() in ("1", "true", "yes"):
        return True
    return os.path.exists(os.path.join(base_dir, config.HALT_FILE))


def position_size(equity, entry, stop_loss):
    """Quantity so that a stop-out loses exactly RISK_PER_TRADE of equity,
    reduced if needed so notional stays within the 3x leverage cap.
    Returns (qty, risk_amount, notional)."""
    dist = abs(entry - stop_loss)
    if dist <= 0 or equity <= 0 or entry <= 0:
        return 0.0, 0.0, 0.0
    qty = (config.RISK_PER_TRADE * equity) / dist
    max_qty = (config.SIM_LEVERAGE * equity) / entry
    qty = min(qty, max_qty)
    return qty, qty * dist, qty * entry


def check_proposal(proposal, state, data_ts_ms, interval, now_ms=None,
                   base_dir="."):
    """state: dict with equity, open_positions, daily_pnl, day_start_equity.
    Returns (approved: bool, reasons: list[str], sizing: dict)."""
    reasons = []
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    if config.MODE != "paper":
        reasons.append("P1: MODE is not 'paper' - refusing to trade at all")
    if kill_switch_active(base_dir):
        reasons.append("P2: kill switch active (HALT file or env)")

    entry = proposal.get("entry")
    sl = proposal.get("stop_loss")
    tp = proposal.get("take_profit")
    side = proposal.get("direction")
    if sl is None or tp is None or entry is None:
        reasons.append("P3: proposal is missing entry, stop loss or take profit")
        sl = tp = None
    elif side == "long" and not (sl < entry < tp):
        reasons.append("P3: long proposal needs stop < entry < take profit")
    elif side == "short" and not (tp < entry < sl):
        reasons.append("P3: short proposal needs take profit < entry < stop")
    elif side not in ("long", "short"):
        reasons.append("P3: unknown direction")

    equity = state.get("equity", 0.0)
    qty, risk_amount, notional = 0.0, 0.0, 0.0
    if sl is not None and entry:
        qty, risk_amount, notional = position_size(equity, entry, sl)
        if qty <= 0:
            reasons.append("P4: position size computed as zero")
        elif risk_amount > config.RISK_PER_TRADE * equity + 1e-9:
            reasons.append(
                f"P4: risk {risk_amount:.2f} exceeds "
                f"{config.RISK_PER_TRADE:.1%} of equity {equity:.2f}")

    if state.get("open_positions", 0) >= config.MAX_OPEN_POSITIONS:
        reasons.append(
            f"P5: already {state.get('open_positions')} open positions (max 2)")

    day_start = state.get("day_start_equity", equity)
    if day_start and state.get("daily_pnl", 0.0) <= -config.MAX_DAILY_LOSS * day_start:
        reasons.append(
            f"P6: daily loss limit hit ({state.get('daily_pnl', 0.0):.2f} vs "
            f"-{config.MAX_DAILY_LOSS * day_start:.2f}); halted for the day")

    from data import interval_ms
    age_ms = now_ms - data_ts_ms
    if age_ms > config.MAX_DATA_AGE_INTERVALS * interval_ms(interval):
        reasons.append(
            f"P7: stale data - last candle is {age_ms / 60000:.0f} min old "
            f"for a {interval} series")

    sizing = {"qty": qty, "risk_amount": round(risk_amount, 2),
              "notional": round(notional, 2),
              "leverage": round(notional / equity, 2) if equity else 0.0}
    return (len(reasons) == 0), reasons, sizing
