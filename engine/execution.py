"""Paper execution engine: a simulator. It never touches any exchange.

Platform copy with the AlphaSwarm execution upgrades (these are NOT in
the frozen forward-run copies):

  * Realistic paper fees: config.FEE_RATE (0.1%) charged on notional at
    entry and on every exit, partial exits included. All stored PnL is
    net of fees.
  * Partial profit booking: at TP1 (entry +/- 1R) half the position is
    closed and the stop moves to break-even (the entry price).
  * Trailing stop: after TP1, the stop trails the best price seen by
    one risk distance (1R).

Exits are detected candle by candle, in order, as new candles arrive.
Within one candle the stop (as it stood when the candle opened) is
checked first - the conservative assumption, as in the original engine.
TP1 is checked before the final take profit. The trailing stop is only
recomputed after a candle completes, so it can never tighten inside
the candle that created the new best price.
"""

import config


def trade_pnl(direction, entry, exit_price, qty):
    if direction == "long":
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def fee_on(price, qty):
    return config.FEE_RATE * price * qty


def entry_fee(entry, qty):
    return fee_on(entry, qty)


def close_at_price(journal, trade, exit_price, ts_ms, reason, detail=""):
    """Close whatever quantity remains at an explicit price (manual
    close, kill switch). PnL is net of the exit fee and of the entry fee
    charged at open; partial bookings already made stay booked.
    Returns the closed-trade summary dict."""
    qty = trade["qty"]
    gross = trade_pnl(trade["direction"], trade["entry_price"],
                      exit_price, qty)
    exit_fee = fee_on(exit_price, qty)
    entry_fee_paid = (trade.get("fees_paid") or 0.0) - sum(
        p.get("fee", 0.0) for p in _partials(trade))
    total_pnl = (trade.get("partial_pnl") or 0.0) + gross \
        - entry_fee_paid - exit_fee
    journal.update_trade(trade["id"],
                         fees_paid=round((trade.get("fees_paid") or 0.0)
                                         + exit_fee, 2))
    journal.close_trade(trade["id"], ts_ms, exit_price, reason,
                        round(total_pnl, 2), detail)
    return {"trade_id": trade["id"], "symbol": trade["symbol"],
            "direction": trade["direction"], "exit_price": exit_price,
            "reason": reason, "pnl": round(total_pnl, 2)}


def _partials(trade):
    import json
    try:
        return json.loads(trade.get("partials") or "[]")
    except ValueError:
        return []


def _stop_reason(trade, stop):
    """Name the kind of stop that fired, for honest reporting."""
    entry = trade["entry_price"]
    if trade["direction"] == "long":
        if stop > entry:
            return "trailing_stop"
        if stop == entry:
            return "break_even_stop"
    else:
        if stop < entry:
            return "trailing_stop"
        if stop == entry:
            return "break_even_stop"
    return "stop_loss"


def _process_trade(journal, trade, candles):
    """Walk one open trade through its candles. Returns a closed-trade
    summary dict, or None if the trade is still open."""
    direction = trade["direction"]
    entry = trade["entry_price"]
    risk_dist = trade.get("risk_dist") or abs(entry - trade["stop_loss"])
    stop = trade["stop_loss"]
    tp = trade["take_profit"]
    tp1 = trade.get("tp1_price")
    tp1_done = bool(trade.get("tp1_done"))
    best = trade.get("best_price") or entry
    qty = trade["qty"]

    for candle in candles:
        if candle["ts"] <= trade["entry_ts_ms"]:
            continue  # only candles after the entry candle may act
        hi, lo = candle["high"], candle["low"]

        # 1) Stop as it stood when this candle opened (conservative).
        if (direction == "long" and lo <= stop) or \
                (direction == "short" and hi >= stop):
            reason = _stop_reason(trade, stop)
            fresh = journal.get_trade(trade["id"])
            detail = (f"Price touched the stop at {stop:.4f} "
                      f"({'trailing stop' if reason == 'trailing_stop' else 'stop loss'}).")
            return close_at_price(journal, fresh, stop, candle["ts"],
                                  reason, detail)

        # 2) TP1: book partial profit, move stop to break-even.
        if tp1 is not None and not tp1_done:
            hit_tp1 = (direction == "long" and hi >= tp1) or \
                      (direction == "short" and lo <= tp1)
            if hit_tp1:
                close_qty = qty * config.PARTIAL_TP_FRACTION
                gross = trade_pnl(direction, entry, tp1, close_qty)
                fee = fee_on(tp1, close_qty)
                journal.record_partial(
                    trade["id"], close_qty, tp1, candle["ts"],
                    gross - fee, fee,
                    f"Booked {config.PARTIAL_TP_FRACTION:.0%} profit at "
                    f"first target {tp1:.4f} (1R); stop moved to "
                    f"break-even at entry {entry:.4f}.")
                qty -= close_qty
                tp1_done = True
                stop = entry  # break-even
                journal.update_trade(trade["id"], tp1_done=1, stop_loss=stop)
                trade = journal.get_trade(trade["id"])

        # 3) Final take profit on what remains.
        if (direction == "long" and hi >= tp) or \
                (direction == "short" and lo <= tp):
            fresh = journal.get_trade(trade["id"])
            detail = f"Price reached the final target at {tp:.4f} (2R)."
            return close_at_price(journal, fresh, tp, candle["ts"],
                                  "take_profit", detail)

        # 4) Trailing: update best price; tighten the stop for the NEXT
        # candle once TP1 has been booked.
        if direction == "long":
            best = max(best, hi)
            if tp1_done and config.TRAILING_ENABLED:
                new_stop = best - risk_dist
                if new_stop > stop:
                    stop = new_stop
        else:
            best = min(best, lo)
            if tp1_done and config.TRAILING_ENABLED:
                new_stop = best + risk_dist
                if new_stop < stop:
                    stop = new_stop
        journal.update_trade(trade["id"], best_price=best, stop_loss=stop)
        trade = journal.get_trade(trade["id"])
    return None


def check_exit(trade, candle):
    """Kept for API compatibility with the original engine's tests:
    simple SL/TP touch check on a single candle."""
    if candle["ts"] <= trade["entry_ts_ms"]:
        return None
    sl, tp = trade["stop_loss"], trade["take_profit"]
    if trade["direction"] == "long":
        if candle["low"] <= sl:
            return sl, "stop_loss"
        if candle["high"] >= tp:
            return tp, "take_profit"
    else:
        if candle["high"] >= sl:
            return sl, "stop_loss"
        if candle["low"] <= tp:
            return tp, "take_profit"
    return None


def watch_exits(journal, candles_by_symbol):
    """Check every open trade against the candles for its symbol.
    Returns a list of closed-trade summaries."""
    closed = []
    for trade in journal.open_trades():
        candles = candles_by_symbol.get(trade["symbol"], [])
        if not candles:
            continue
        summary = _process_trade(journal, trade, candles)
        if summary:
            closed.append(summary)
    if closed:
        journal.log_equity(journal.equity(), "exit watcher")
    return closed
