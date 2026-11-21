"""Stage C (v3) - event-driven paper rules (RESEARCH).

One deliberately pessimistic rule, tested with labelled sample events and
left collecting forward data - NOT backtested, because point-in-time
historical news cannot be reconstructed honestly from live feeds.

The rule:
  TRIGGER   an event whose category is EXCHANGE_LISTING, or whose typed
            sentiment is strong_positive (see events.classify), AND which
            names at least one pair symbol, AND whose confirmation status
            is CONFIRMED (official source, or two independent credible
            sources - see events.assign_confirmations). UNCONFIRMED
            rumors are logged but NEVER trigger an entry.
  DELAY     no entry in the event's own hour. The entry candle is the
            first 1h candle that OPENS at or after the next hour boundary
            following the event's effective time (when this system saw
            the event - point-in-time honest).
  SLIPPAGE  the fill price is that candle's OPEN x (1 + 2%) for a long -
            the strategy pays up for being late, on purpose.
  GEOMETRY  stop = entry - 1.5 x ATR(14 at the entry candle);
            take profit = entry + 2R. Long only: this stage does not
            short bad news (delistings/hacks are logged, not traded).
  GATE      the ordinary risk policy (policy.check_proposal) must still
            approve: 1% risk, max 2 open positions, daily-loss halt,
            kill switch, mandatory SL/TP. The policy's staleness check
            runs against the decision moment (the entry candle's close
            in simulations; real time in live cycles), so an event whose
            entry candle is already old news gets rejected as stale
            instead of being chased.

Deduplication: an (event_id, symbol) pair is attempted at most once to a
final state (opened / rejected). "pending" (entry candle not in the data
yet) leaves no record and is retried on the next cycle. Attempts are
tracked in an `event_entries` table inside the journal database.
"""

import sqlite3  # noqa: F401  (kept for type clarity in ensure_table)
import time

import config
import indicators
import policy

HOUR_MS = 3_600_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_entries (
    event_id TEXT, symbol TEXT, status TEXT, trade_id INTEGER,
    ts_ms INTEGER, detail TEXT,
    PRIMARY KEY (event_id, symbol)
);
"""


def ensure_table(journal):
    journal.conn.executescript(_SCHEMA)
    journal.conn.commit()


def is_trigger(event):
    if event.get("confirmation") != "CONFIRMED":
        return False  # rumors are logged, never traded
    return bool(event.get("symbols")) and (
        event.get("category") == "EXCHANGE_LISTING"
        or event.get("strong_positive"))


def next_entry_candle(event_ts_ms, candles):
    """First candle opening at/after the next hour boundary strictly
    following the event. Returns (index, candle) or (None, None)."""
    boundary = (event_ts_ms // HOUR_MS + 1) * HOUR_MS
    for i, c in enumerate(candles):
        if c["ts"] >= boundary:
            return i, c
    return None, None


def _already_attempted(journal, event_id, symbol):
    ensure_table(journal)
    row = journal.conn.execute(
        "SELECT status FROM event_entries WHERE event_id=? AND symbol=?",
        (event_id, symbol)).fetchone()
    return row["status"] if row else None


def _record(journal, event, symbol, status, trade_id, detail):
    journal.conn.execute(
        "INSERT OR REPLACE INTO event_entries (event_id, symbol, status,"
        " trade_id, ts_ms, detail) VALUES (?,?,?,?,?,?)",
        (event["event_id"], symbol, status, trade_id,
         int(time.time() * 1000), detail))
    journal.conn.commit()


def record_attempt(journal, event, symbol, status, detail):
    """Public wrapper so the live cycle can log non-entry outcomes
    (e.g. skipped_backlog) into the same attempt table."""
    ensure_table(journal)
    _record(journal, event, symbol, status, None, detail)


def attempt_event_entry(journal, event, symbol, candles, now_ms=None):
    """Try to open ONE paper position for (event, symbol) from 1h candles.
    Returns a dict with status in {not_trigger, duplicate, pending,
    rejected, opened} plus details. Never raises for expected cases."""
    ensure_table(journal)
    if not is_trigger(event) or symbol not in event.get("symbols", []):
        return {"status": "not_trigger", "symbol": symbol}

    prev = _already_attempted(journal, event["event_id"], symbol)
    if prev in ("opened", "rejected"):
        return {"status": "duplicate", "symbol": symbol, "prior": prev}

    idx, candle = next_entry_candle(event["effective_ts_ms"], candles)
    if candle is None:
        return {"status": "pending", "symbol": symbol,
                "reason": "entry candle (next hour after the event) "
                          "not in the data yet"}

    series = indicators.compute_series(candles[:idx + 1])
    atr = series["atr14"][idx]
    if not atr or atr <= 0:
        _record(journal, event, symbol, "rejected", None,
                "no ATR at entry candle")
        return {"status": "rejected", "symbol": symbol,
                "reasons": ["no ATR available at the entry candle"]}

    entry = candle["open"] * (1.0 + config.EVENT_SLIPPAGE)
    risk_dist = config.EVENT_ATR_SL_MULTIPLIER * atr
    proposal = {
        "symbol": symbol,
        "direction": "long",
        "entry": round(entry, 6),
        "stop_loss": round(entry - risk_dist, 6),
        "take_profit": round(entry + config.EVENT_TP_R_MULTIPLE
                             * risk_dist, 6),
        "avg_confidence": 0.65,  # fixed: event trigger, not agent consensus
    }
    # Decision moment: close of the entry candle, unless the caller
    # (live cycle) supplies real time.
    decision_ms = now_ms if now_ms is not None else candle["ts"] + HOUR_MS
    state = journal.daily_state(now_ms=decision_ms)
    approved, reasons, sizing = policy.check_proposal(
        proposal, state, candle["ts"], config.PRIMARY_INTERVAL,
        now_ms=decision_ms)

    cycle_id = journal.log_cycle(
        symbol, config.PRIMARY_INTERVAL, "event", candle["open"],
        "event_entry_opened" if approved else "event_entry_rejected",
        f"event {event['event_id']} [{event['category']}] "
        f"{event['headline'][:80]}")
    if not approved:
        journal.log_proposal(cycle_id, proposal, "rejected", reasons)
        _record(journal, event, symbol, "rejected", None,
                "; ".join(reasons))
        return {"status": "rejected", "symbol": symbol,
                "reasons": reasons, "proposal": proposal}

    pid = journal.log_proposal(
        cycle_id, proposal, "approved",
        [f"event:{event['event_id']}",
         f"category:{event['category']}",
         f"slippage:{config.EVENT_SLIPPAGE:.0%}"])
    trade_id = journal.open_trade(pid, proposal, sizing["qty"],
                                  sizing["risk_amount"], candle["ts"])
    journal.log_equity(state["equity"], "event trade opened")
    _record(journal, event, symbol, "opened", trade_id,
            f"entry={proposal['entry']} (candle open {candle['open']} "
            f"+ {config.EVENT_SLIPPAGE:.0%} slippage)")
    return {"status": "opened", "symbol": symbol, "trade_id": trade_id,
            "proposal": proposal, "entry_candle_ts": candle["ts"]}
