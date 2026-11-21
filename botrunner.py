"""Bot lifecycle for the AlphaSwarm platform.

Each bot owns a directory data/bots/<id>/ with its own engine journal
(journal.db) and its own HALT file (the engine's kill-switch input).
Bots run the copied AlphaPaper engine in-process: because the engine
keeps its settings in a module-level config, every cycle runs under a
single global lock with the bot's values patched into config (and
restored afterwards). Cycles are therefore serialised - simple and
safe for a single-process platform.

PAPER TRADING ONLY. Nothing here places a real order; there is no
real-order code path anywhere in this project.
"""

import contextlib
import io
import json
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.join(BASE_DIR, "engine")
DATA_DIR = os.environ.get(
    "ALPHASWARM_DATA_DIR", os.path.join(BASE_DIR, "data"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

import config  # noqa: E402  (engine config)
import data as market  # noqa: E402
import indicators  # noqa: E402
from agents import run_agents  # noqa: E402
from execution import trade_pnl as execution_pnl  # noqa: E402
from journal import Journal  # noqa: E402

ENGINE_LOCK = threading.RLock()

_PATCH_KEYS = ("SYMBOLS", "STRATEGY", "START_EQUITY", "RISK_PER_TRADE",
               "MAX_DAILY_LOSS", "EVENT_LOG_PATH", "ALERTS_LOG_PATH",
               "SCREENER_REPORT_PATH", "SCREENER_UNIVERSE_PATH")


def bot_dir(bot_id):
    path = os.path.join(DATA_DIR, "bots", str(bot_id))
    os.makedirs(path, exist_ok=True)
    return path


def journal_path(bot_id):
    return os.path.join(bot_dir(bot_id), "journal.db")


def _now_ms():
    return int(time.time() * 1000)


@contextlib.contextmanager
def patched_config(bot):
    """Patch engine config with this bot's settings; restore on exit.
    Must be held together with ENGINE_LOCK (callers do)."""
    saved = {k: getattr(config, k) for k in _PATCH_KEYS}
    try:
        config.SYMBOLS = json.loads(bot["symbols"])
        config.STRATEGY = bot["kind"] if bot["kind"] in ("v1", "v2") else "v1"
        config.START_EQUITY = float(bot["start_equity"])
        config.RISK_PER_TRADE = float(bot["risk_pct"])
        config.MAX_DAILY_LOSS = float(bot["daily_loss_pct"])
        config.EVENT_LOG_PATH = os.path.join(DATA_DIR, "events.jsonl")
        config.ALERTS_LOG_PATH = os.path.join(DATA_DIR, "alerts.jsonl")
        config.SCREENER_REPORT_PATH = os.path.join(DATA_DIR,
                                                   "screener_report.md")
        config.SCREENER_UNIVERSE_PATH = os.path.join(DATA_DIR,
                                                     "screener_universe.json")
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def open_journal(bot):
    return Journal(journal_path(bot["id"]))


def init_bot_journal(bot):
    with ENGINE_LOCK, patched_config(bot):
        journal = open_journal(bot)
        try:
            journal.log_equity(float(bot["start_equity"]),
                               "paper account created")
        finally:
            journal.close()


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------

def run_cycle(db, bot):
    """Run one cycle for a bot. Returns a summary string. Never raises
    for expected failures (network down -> engine's sample fallback;
    errors are captured into the summary)."""
    if db.get_setting("global_halt") == "1":
        return "Global kill switch is ON - cycle skipped."
    if bot["status"] == "stopped":
        return "Bot is stopped - cycle skipped."
    with ENGINE_LOCK, patched_config(bot):
        journal = open_journal(bot)
        try:
            if bot["kind"] == "event":
                summary = _run_event_cycle(journal)
            elif bot["kind"] == "breakout":
                summary = _run_breakout_cycle(bot, journal)
            elif bot["kind"] == "router":
                summary = _run_router_cycle(bot, journal)
            elif bot["kind"] == "range":
                summary = _run_range_cycle(bot, journal)
            else:
                summary = _run_technical_cycle(bot, journal)
            journal.log_equity(journal.equity(), "cycle")
        except Exception as exc:  # keep the platform alive; report honestly
            summary = f"Cycle failed: {type(exc).__name__}: {exc}"
        finally:
            journal.close()
    db.update_bot(bot["id"], last_cycle_at=_now_ms(),
                  last_cycle_summary=summary)
    return summary


def _run_technical_cycle(bot, journal):
    """One technical (v1/v2) cycle. In universe mode "screener" the
    analysis set is first rebuilt from the all-coin screener; in mode
    "fixed" the bot's configured symbol list is used, exactly as
    before. The risk policy is identical either way - a wider universe
    only means more candidates competing for the same max-2 slots."""
    import engine as engine_mod
    universe_note = ""
    if bot.get("universe_mode") == "screener":
        ok, note = _apply_screener_universe(bot, journal)
        if not ok:
            return note  # failed closed; failure already journaled
        universe_note = note
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        results = engine_mod.run_cycle(
            journal, base_dir=bot_dir(bot["id"]))
    opened = sum(1 for r in results if r["action"] == "trade_opened")
    rejected = sum(1 for r in results
                   if r["action"] == "proposal_rejected")
    summary = (f"Cycle done: {len(results)} symbols checked, "
               f"{opened} paper trade(s) opened, "
               f"{rejected} proposal(s) rejected.")
    if universe_note:
        summary = universe_note + " " + summary
    out = buf.getvalue().strip()
    if out:
        summary += " " + out.splitlines()[-1][:200]
    return summary


def _apply_screener_universe(bot, journal):
    """Point the engine at this cycle's screener trading universe:
    the top `universe_size` ranked coins (published screener score,
    $5M volume floor, stable/fiat + leveraged-token filters), with
    tokenized-stock (bStock) pairs and XAUT (gold) removed, and
    BTC/ETH/SOL always included. The chosen universe is journaled as
    a UNIVERSE cycle row so every trade can be traced to the universe
    that produced it. Analysis cap: universe_size + 3 majors.

    Returns (True, note) on success. On ANY screener failure returns
    (False, summary): the failure is journaled and the caller opens
    nothing - fail closed, never a stale-data fallback."""
    import screener
    size = bot.get("universe_size") or 20
    size = max(5, min(30, int(size)))
    try:
        uni = screener.build_trading_universe(top_n=size)
    except Exception as exc:
        detail = (f"screener fetch failed ({type(exc).__name__}: {exc}); "
                  "cycle aborted, nothing opened (fail-closed, no "
                  "stale-data fallback)")
        journal.log_cycle("UNIVERSE", config.PRIMARY_INTERVAL, "screener",
                          0, "universe_failed", detail)
        return False, f"Cycle failed closed: {detail}"
    config.SYMBOLS = uni["symbols"]
    detail = (f"mode=screener size={size} "
              f"eligible={uni['eligible_count']} "
              f"excluded_non_crypto="
              f"{','.join(uni['excluded_non_crypto']) or 'none'} "
              f"symbols={','.join(uni['symbols'])}")
    journal.log_cycle("UNIVERSE", config.PRIMARY_INTERVAL, "screener", 0,
                      "universe", detail)
    note = (f"Universe (screener top {size} + majors, "
            f"{len(uni['symbols'])} coins): "
            f"{', '.join(uni['symbols'])}.")
    return True, note


def _run_breakout_cycle(bot, journal):
    """One breakout cycle. The signal source is the Donchian-20
    breakout rule (engine_breakout), not the analyst-agent consensus;
    the risk policy, execution upgrades and fees are the platform's,
    identical to the other trading bots. Universe mode "screener"
    works exactly as for technical bots (fail closed on a screener
    failure); mode "fixed" uses the bot's configured symbol list."""
    import engine_breakout
    universe_note = ""
    if bot.get("universe_mode") == "screener":
        ok, note = _apply_screener_universe(bot, journal)
        if not ok:
            return note  # failed closed; failure already journaled
        universe_note = note
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        results = engine_breakout.run_cycle(
            journal, base_dir=bot_dir(bot["id"]))
    opened = sum(1 for r in results if r["action"] == "trade_opened")
    rejected = sum(1 for r in results
                   if r["action"] == "proposal_rejected")
    summary = (f"Cycle done: {len(results)} symbols checked, "
               f"{opened} paper trade(s) opened, "
               f"{rejected} proposal(s) rejected.")
    if universe_note:
        summary = universe_note + " " + summary
    out = buf.getvalue().strip()
    if out:
        summary += " " + out.splitlines()[-1][:200]
    return summary


def _run_router_cycle(bot, journal):
    """One Regime Router cycle. Each symbol's market situation is
    classified first (engine/regime.py); the entry decision is then
    delegated to the specialist with a proven record in that
    situation - breakout for TREND_EXPANSION, the v2-gated analyst
    consensus for CLEAN_TREND - or the router sits out (COMPRESSION /
    RANGING / MIXED_UNCLEAR have no proven specialist yet). The risk
    policy, execution upgrades and fees are the platform's, identical
    to the other trading bots. Universe mode "screener" works exactly
    as for technical and breakout bots (fail closed on a screener
    failure); mode "fixed" uses the bot's configured symbol list."""
    import engine_router
    universe_note = ""
    if bot.get("universe_mode") == "screener":
        ok, note = _apply_screener_universe(bot, journal)
        if not ok:
            return note  # failed closed; failure already journaled
        universe_note = note
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        results = engine_router.run_cycle(
            journal, base_dir=bot_dir(bot["id"]))
    opened = sum(1 for r in results if r["action"] == "trade_opened")
    rejected = sum(1 for r in results
                   if r["action"] == "proposal_rejected")
    sat_out = sum(1 for r in results if r["action"] == "regime_no_trade")
    tally = {}
    for r in results:
        if r.get("regime"):
            tally[r["regime"]] = tally.get(r["regime"], 0) + 1
    regime_note = ", ".join(f"{k} {v}" for k, v in sorted(tally.items()))
    summary = (f"Cycle done: {len(results)} symbols checked, "
               f"{opened} paper trade(s) opened, "
               f"{rejected} proposal(s) rejected, "
               f"{sat_out} sat out by regime. "
               f"Regimes: {regime_note}.")
    if universe_note:
        summary = universe_note + " " + summary
    out = buf.getvalue().strip()
    if out:
        summary += " " + out.splitlines()[-1][:200]
    return summary


def _run_range_cycle(bot, journal):
    """One Range Specialist (candidate) cycle. The signal source is
    the stochastic range fade (engine_range): the specialist's own
    ranging self-gate (ADX < 20, flat EMA50 slope, established
    48-bar channel >= 3xATR wide) must pass before any stochastic
    turn out of an extreme zone can become a proposal, and every
    gate-closed sit-out is journaled with its measures. The risk
    policy, execution upgrades and fees are the platform's,
    identical to the other trading bots. CANDIDATE: unproven,
    judged on forward paper data only; not in the Regime Router's
    admitted roster. Universe mode "screener" works exactly as for
    the other trading bots (fail closed on a screener failure);
    mode "fixed" uses the bot's configured symbol list."""
    import engine_range
    universe_note = ""
    if bot.get("universe_mode") == "screener":
        ok, note = _apply_screener_universe(bot, journal)
        if not ok:
            return note  # failed closed; failure already journaled
        universe_note = note
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        results = engine_range.run_cycle(
            journal, base_dir=bot_dir(bot["id"]))
    opened = sum(1 for r in results if r["action"] == "trade_opened")
    rejected = sum(1 for r in results
                   if r["action"] == "proposal_rejected")
    sat_out = sum(1 for r in results if r["action"] == "range_no_trade")
    summary = (f"Cycle done: {len(results)} symbols checked, "
               f"{opened} paper trade(s) opened, "
               f"{rejected} proposal(s) rejected, "
               f"{sat_out} sat out (range gate closed).")
    if universe_note:
        summary = universe_note + " " + summary
    out = buf.getvalue().strip()
    if out:
        summary += " " + out.splitlines()[-1][:200]
    return summary


def _run_event_cycle(journal):
    """Event Watcher cycle: screener refresh + news poll + alerts.
    This bot never trades; it watches and reports."""
    import alerts
    import events
    import screener
    parts = []
    try:
        result = screener.run_screener()
        top = ", ".join(r["symbol"] for r in result["top"][:5])
        parts.append(f"Screener: {len(result['eligible'])} eligible coins, "
                     f"top 5: {top}.")
        universe = {r["base"]: r["symbol"] for r in result["eligible"]}
    except Exception as exc:
        parts.append(f"Screener failed ({type(exc).__name__}); using the "
                     f"last known coin list.")
        try:
            universe = screener.load_universe()
        except Exception:
            # No cached coin list either (fresh data dir) and the
            # live fetch inside load_universe failed too: poll the
            # news with an empty symbol dictionary rather than
            # killing the whole cycle.
            universe = {}
            parts.append("No cached coin list available either; the "
                         "news poll continues without one.")
    new_events, status = events.poll_events(universe,
                                            path=config.EVENT_LOG_PATH)
    ok = sum(1 for s in status.values() if s[0] == "ok")
    parts.append(f"News: {len(new_events)} new event(s) from "
                 f"{ok}/{len(status)} sources.")
    pool = events.load_events(path=config.EVENT_LOG_PATH)
    written = alerts.process_alerts(pool, path=config.ALERTS_LOG_PATH)
    if written:
        parts.append(f"{len(written)} new CONFIRMED alert(s).")
    fg = events.get_fear_greed()
    if fg:
        parts.append(f"Fear & Greed: {fg['value']} ({fg['label']}).")
    journal.log_cycle("NEWS", "event", "platform", 0, "event_poll",
                      " ".join(parts))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Positions: demo/test open, manual close, kill switches
# ---------------------------------------------------------------------------

def open_demo_position(db, bot, symbol=None):
    """Open a clearly-labelled TEST paper position through the real
    risk-policy gate, using the live (or sample-fallback) snapshot and
    the agents' actual current votes. Exists so the owner can verify
    the full trade -> reason -> exit pipeline end to end on demand;
    the stored reason says plainly that this is a test position, not a
    strategy signal."""
    import engine as engine_mod
    import policy
    from execution import entry_fee
    symbol = symbol or json.loads(bot["symbols"])[0]
    with ENGINE_LOCK, patched_config(bot):
        journal = open_journal(bot)
        try:
            candles, source = market.get_candles(
                symbol, config.PRIMARY_INTERVAL)
            htf, _ = market.get_candles(symbol, config.HTF_INTERVAL)
            snap = indicators.compute_snapshot(candles, htf)
            votes = run_agents(snap)
            atr = snap["atr14"]
            if not atr:
                return {"ok": False,
                        "detail": "Not enough market data yet (ATR warm-up)."}
            # Direction: follow the majority agent vote; default long.
            longs = sum(1 for v in votes if v.direction == "long")
            shorts = sum(1 for v in votes if v.direction == "short")
            direction = "short" if shorts > longs else "long"
            entry = snap["price"]
            dist = config.ATR_SL_MULTIPLIER * atr
            if direction == "long":
                stop, tp = entry - dist, entry + config.TP_R_MULTIPLE * dist
            else:
                stop, tp = entry + dist, entry - config.TP_R_MULTIPLE * dist
            proposal = {"symbol": symbol, "direction": direction,
                        "entry": round(entry, 4),
                        "stop_loss": round(stop, 4),
                        "take_profit": round(tp, 4),
                        "avg_confidence": 0.0, "agreeing_agents": [],
                        "votes": [v.to_dict() for v in votes]}
            state = journal.daily_state()
            approved, reasons, sizing = policy.check_proposal(
                proposal, state, snap["last_ts"], config.PRIMARY_INTERVAL,
                base_dir=bot_dir(bot["id"]))
            if not approved:
                return {"ok": False,
                        "detail": "Risk Manager rejected the test "
                                  "position: " + "; ".join(reasons)}
            vote_lines = "\n".join(
                f"- {v.agent}: {v.direction} (confidence "
                f"{v.confidence:.0%}) - {'; '.join(v.evidence[:2])}"
                for v in votes)
            reason = (
                "TEST POSITION - opened manually by the admin to verify "
                "the platform end to end. This is NOT a strategy signal. "
                f"The analyst agents' actual votes at the time were:\n"
                f"{vote_lines}\nRisk Manager approved it through the "
                f"normal gate: risking ${sizing['risk_amount']:.2f} "
                f"({config.RISK_PER_TRADE:.0%} of equity). Paper trade - "
                "no real money.")
            cycle_id = journal.log_cycle(
                symbol, config.PRIMARY_INTERVAL, source, snap["price"],
                "test_position_opened",
                "admin demo/test position (not a strategy signal)")
            journal.log_votes(cycle_id, symbol,
                              [v.to_dict() for v in votes])
            pid = journal.log_proposal(cycle_id, proposal, "approved",
                                       ["admin test position"])
            trade_id = journal.open_trade(
                pid, proposal, sizing["qty"], sizing["risk_amount"],
                snap["last_ts"], entry_reason=reason,
                tp1_price=engine_mod.tp1_price(proposal),
                entry_fee=round(entry_fee(entry, sizing["qty"]), 2))
            journal.log_equity(state["equity"], "test position opened")
            return {"ok": True, "trade_id": trade_id,
                    "detail": f"Test position #{trade_id} opened: "
                              f"{direction.upper()} {symbol} @ {entry:.4f} "
                              f"(data source: {source})."}
        finally:
            journal.close()


def _latest_price(symbol):
    candles, _ = market.get_candles(symbol, config.PRIMARY_INTERVAL)
    if candles:
        return candles[-1]["close"], candles[-1]["ts"]
    return None, None


def close_position(db, bot, trade_id, reason, detail):
    """Close one open paper position at the latest available price."""
    from execution import close_at_price
    with ENGINE_LOCK, patched_config(bot):
        journal = open_journal(bot)
        try:
            trade = journal.get_trade(trade_id)
            if not trade or trade["status"] != "open":
                return {"ok": False, "detail": "Position not open."}
            price, ts = _latest_price(trade["symbol"])
            if price is None:
                price, ts = trade["entry_price"], _now_ms()
            summary = close_at_price(journal, trade, price,
                                     ts or _now_ms(), reason, detail)
            journal.log_equity(journal.equity(), f"position closed: {reason}")
            return {"ok": True, "summary": summary}
        finally:
            journal.close()


def kill_bot(db, bot, by="admin"):
    """Per-bot kill switch: close every open paper position now, stop
    the bot, and drop the engine HALT file so nothing can reopen."""
    closed = []
    with ENGINE_LOCK, patched_config(bot):
        journal = open_journal(bot)
        try:
            from execution import close_at_price
            for trade in journal.open_trades():
                price, ts = _latest_price(trade["symbol"])
                if price is None:
                    price, ts = trade["entry_price"], _now_ms()
                closed.append(close_at_price(
                    journal, trade, price, ts or _now_ms(), "kill_switch",
                    f"Kill switch activated by {by}: all paper positions "
                    "closed immediately."))
            if closed:
                journal.log_equity(journal.equity(), "kill switch")
        finally:
            journal.close()
    halt = os.path.join(bot_dir(bot["id"]), config.HALT_FILE)
    with open(halt, "w") as f:
        f.write(f"kill switch by {by} at {_now_ms()}\n")
    db.update_bot(bot["id"], status="stopped",
                  last_cycle_summary=f"Kill switch: {len(closed)} open "
                                     "position(s) closed.")
    return closed


def clear_bot_halt(bot):
    halt = os.path.join(bot_dir(bot["id"]), config.HALT_FILE)
    if os.path.exists(halt):
        os.remove(halt)


def global_kill(db, by="admin"):
    """Global kill switch: halt everything and close every open paper
    position on every bot."""
    db.set_setting("global_halt", "1")
    total = []
    for bot in db.list_bots():
        if bot["kind"] != "event":
            total += kill_bot(db, bot, by=by)
        else:
            db.update_bot(bot["id"], status="stopped")
    return total


def global_clear(db):
    db.set_setting("global_halt", "0")
    for bot in db.list_bots():
        clear_bot_halt(bot)


# ---------------------------------------------------------------------------
# In-app scheduler (no OS cron): polls every 15 seconds and runs any
# running bot whose configured interval has elapsed.
# ---------------------------------------------------------------------------

def scheduler_tick(db):
    """One scheduler pass: run every due bot. Each bot is wrapped
    individually, so one bot's exception can never skip or kill the
    other bots in the same pass (run_cycle itself already converts
    engine errors into a summary; this is the second line of defence
    for failures in the scheduler's own bookkeeping)."""
    if db.get_setting("global_halt") == "1":
        return
    for bot in db.list_bots():
        try:
            if bot["status"] != "running":
                continue
            minutes = bot["cycle_minutes"] or 0
            if minutes <= 0:
                continue
            last = bot["last_cycle_at"] or 0
            if _now_ms() - last >= minutes * 60_000:
                run_cycle(db, db.get_bot(bot["id"]))
        except Exception:
            # A single bad bot must not stop the others.
            continue


def scheduler_loop(db, stop_event):
    while not stop_event.wait(15):
        try:
            scheduler_tick(db)
        except Exception:
            # The scheduler must never take the platform down.
            continue
