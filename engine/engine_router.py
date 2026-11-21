"""One Regime Router cycle: fetch data -> watch exits -> classify
each symbol's market situation -> hand the entry decision to the
specialist that has PROVEN itself in that situation -> risk policy
gate -> simulated fill.

Routing table (the specialists are the platform's existing engines;
nothing new decides entries here):

  TREND_EXPANSION  -> the Breakout specialist (engine/breakout.py's
                      Donchian-20 signal, exactly as the breakout bot
                      runs it). Breakout is the only strategy in this
                      project that passed the strict after-fees,
                      both-windows, no-single-symbol-carry bar.
  CLEAN_TREND      -> the v2-gated analyst consensus (the four agents
                      + decision.build_proposal + strategy_v2's hard
                      4h-trend filter and Momentum requirement),
                      exactly as the Technical v2 bot runs it.
  COMPRESSION      -> NO TRADE. The squeeze candidate was a near-miss
  RANGING             that failed the bar, and the mean-reversion
  MIXED_UNCLEAR       candidate failed outright; ranging has no
                      proven specialist yet. Sitting out is the
                      correct behaviour, and the decision is
                      journaled with its reason like every other.

Every symbol, every cycle, gets a journal row carrying the regime
label, the measures behind it, and which specialist was consulted or
why the router sat out - the same audit standard as the platform's
per-trade plain-language reasons. Any trade the router opens goes
through the unchanged platform path: the deterministic risk policy
(<= configured risk % per trade, max 2 open, daily-loss halt,
stale-data refusal, kill switch), the 0.1%-per-side paper fees, and
the platform execution upgrades (TP1 partial booking at 1R,
break-even stop move, trailing stop) via execution.watch_exits.

Honest limitation: the classifier's thresholds are principled, not
tuned (see regime.py), but whether ROUTING adds anything over running
the specialists directly can only be proven by this bot's forward
paper record, which starts from zero.
"""

import config
import data as market
import indicators
import policy
import regime
from agents import run_agents
from breakout import breakout_signal
from decision import build_proposal
from engine import build_entry_reason, tp1_price
from engine_breakout import build_breakout_reason
from execution import entry_fee, watch_exits
from htf import HTFTrend
from strategy_v2 import filter_proposal

HOUR_MS = 3_600_000

SIT_OUT = ("COMPRESSION", "RANGING", "MIXED_UNCLEAR")

SPECIALIST = {
    "TREND_EXPANSION": "Breakout specialist",
    "CLEAN_TREND": "v2-gated analyst consensus",
}


def _router_prefix(r, specialist_name):
    """Plain-language routing explanation, prepended to the
    specialist's own trade reason. Built only from the actual
    classification numbers - nothing invented."""
    return (
        f"ROUTED BY THE REGIME ROUTER: for this coin the router "
        f"classified the market situation as "
        f"{r['label'].replace('_', ' ')} - {r['plain']} "
        f"(efficiency ratio {r['er']}, trend strength ADX "
        f"{r['adx']}, volatility percentile {r['bbw_pct']}). The "
        f"router handed the entry decision to the {specialist_name}, "
        f"the specialist with a proven record in this situation; the "
        f"specialist's own reasoning follows.\n")


def _open_breakout(journal, base_dir, symbol, candles, htf_candles,
                   source, r):
    """TREND_EXPANSION branch: the breakout specialist decides.
    Mirrors engine_breakout.run_cycle's per-symbol flow (same signal,
    same dedupe, same policy gate, same geometry)."""
    price = candles[-1]["close"]
    signal_ts = candles[-1]["ts"]
    atr_values = indicators.atr_series(candles, 14)
    trend = HTFTrend(htf_candles).at(signal_ts + HOUR_MS)
    raw, why = breakout_signal(candles, len(candles) - 1, atr_values,
                               trend)
    head = (f"regime={r['label']} {r['measures']} | routed to "
            f"Breakout specialist")
    if raw is None:
        detail = f"{head}: breakout={why} 4h_trend={trend}"
        journal.log_cycle(symbol, config.PRIMARY_INTERVAL, source,
                          price, "no_proposal", detail)
        print(f"{symbol} [{source}] {r['label']} -> breakout "
              f"specialist: {why} (trend={trend}) -> no proposal")
        return {"symbol": symbol, "action": "no_proposal",
                "regime": r["label"]}
    proposal = {"symbol": symbol, "direction": raw["direction"],
                "entry": raw["entry"], "stop_loss": raw["stop_loss"],
                "take_profit": raw["take_profit"],
                "avg_confidence": None}
    detail = (f"{head} | breakout {raw['direction']} channel "
              f"[{raw['channel_low']:.2f}, {raw['channel_high']:.2f}] "
              f"atr={raw['atr']:.4f} atr_pct={raw['atr_pct']} "
              f"4h_trend={trend}")
    if journal.trade_exists_for_signal(symbol, signal_ts):
        journal.log_cycle(symbol, config.PRIMARY_INTERVAL, source,
                          price, "no_proposal",
                          detail + " | already acted on this signal bar")
        print(f"{symbol} [{source}] breakout {raw['direction']} "
              f"already acted on for this signal bar -> skipped")
        return {"symbol": symbol, "action": "no_proposal",
                "regime": r["label"]}
    state = journal.daily_state()
    approved, reasons, sizing = policy.check_proposal(
        proposal, state, signal_ts, config.PRIMARY_INTERVAL,
        base_dir=base_dir)
    if not approved:
        cycle_id = journal.log_cycle(
            symbol, config.PRIMARY_INTERVAL, source, price,
            "proposal_rejected", f"{detail} | " + "; ".join(reasons))
        journal.log_proposal(cycle_id, proposal, "rejected", reasons)
        print(f"{symbol} [{source}] proposal REJECTED by policy: "
              f"{'; '.join(reasons)}")
        return {"symbol": symbol, "action": "proposal_rejected",
                "reasons": reasons, "regime": r["label"]}
    fee = round(entry_fee(proposal["entry"], sizing["qty"]), 2)
    cycle_id = journal.log_cycle(
        symbol, config.PRIMARY_INTERVAL, source, price, "trade_opened",
        f"{detail} | qty={sizing['qty']:.6f} "
        f"risk={sizing['risk_amount']:.2f} "
        f"notional={sizing['notional']:.2f} "
        f"lev={sizing['leverage']:.2f}x entry_fee={fee:.2f}")
    pid = journal.log_proposal(cycle_id, proposal, "approved", [])
    reason_text = (_router_prefix(r, SPECIALIST["TREND_EXPANSION"])
                   + build_breakout_reason(raw, trend, sizing,
                                           state["equity"]))
    trade_id = journal.open_trade(
        pid, proposal, sizing["qty"], sizing["risk_amount"], signal_ts,
        entry_reason=reason_text, tp1_price=tp1_price(proposal),
        entry_fee=fee)
    journal.log_equity(state["equity"], "trade opened")
    print(f"{symbol} [{source}] {r['label']} -> breakout "
          f"{proposal['direction'].upper()} @ {proposal['entry']} "
          f"-> PAPER TRADE #{trade_id} OPENED")
    return {"symbol": symbol, "action": "trade_opened",
            "trade_id": trade_id, "regime": r["label"]}


def _open_consensus(journal, base_dir, symbol, candles, htf_candles,
                    source, r):
    """CLEAN_TREND branch: the v2-gated analyst consensus decides.
    Mirrors engine.run_cycle's per-symbol flow under STRATEGY=v2
    (agents vote -> consensus proposal -> v2 filter -> policy gate)."""
    price = candles[-1]["close"]
    head = (f"regime={r['label']} {r['measures']} | routed to "
            f"v2-gated analyst consensus")
    snap = indicators.compute_snapshot(candles, htf_candles)
    votes = run_agents(snap)
    vote_dicts = [v.to_dict() for v in votes]
    proposal = build_proposal(symbol, snap, votes)
    if proposal is None:
        tally = {}
        for v in votes:
            tally[v.direction] = tally.get(v.direction, 0) + 1
        cycle_id = journal.log_cycle(
            symbol, config.PRIMARY_INTERVAL, source, price,
            "no_proposal",
            f"{head}: the analyst agents produced no consensus "
            f"proposal (vote tally {tally})")
        journal.log_votes(cycle_id, symbol, vote_dicts)
        print(f"{symbol} [{source}] {r['label']} -> v2 consensus: "
              f"votes={tally} -> no proposal")
        return {"symbol": symbol, "action": "no_proposal",
                "regime": r["label"]}
    ok_v2, reason_v2 = filter_proposal(proposal, snap, votes)
    if not ok_v2:
        cycle_id = journal.log_cycle(
            symbol, config.PRIMARY_INTERVAL, source, price,
            "proposal_rejected",
            f"{head} | strategy v2 filter: {reason_v2}")
        journal.log_votes(cycle_id, symbol, vote_dicts)
        journal.log_proposal(cycle_id, proposal, "rejected",
                             [f"strategy v2 filter: {reason_v2}"])
        print(f"{symbol} [{source}] proposal BLOCKED by strategy v2 "
              f"filter: {reason_v2}")
        return {"symbol": symbol, "action": "proposal_rejected",
                "reasons": [reason_v2], "regime": r["label"]}
    state = journal.daily_state()
    approved, reasons, sizing = policy.check_proposal(
        proposal, state, snap["last_ts"], config.PRIMARY_INTERVAL,
        base_dir=base_dir)
    if not approved:
        cycle_id = journal.log_cycle(
            symbol, config.PRIMARY_INTERVAL, source, price,
            "proposal_rejected", f"{head} | " + "; ".join(reasons))
        journal.log_votes(cycle_id, symbol, vote_dicts)
        journal.log_proposal(cycle_id, proposal, "rejected", reasons)
        print(f"{symbol} [{source}] proposal REJECTED by policy: "
              f"{'; '.join(reasons)}")
        return {"symbol": symbol, "action": "proposal_rejected",
                "reasons": reasons, "regime": r["label"]}
    fee = round(entry_fee(proposal["entry"], sizing["qty"]), 2)
    cycle_id = journal.log_cycle(
        symbol, config.PRIMARY_INTERVAL, source, price, "trade_opened",
        f"{head} | qty={sizing['qty']:.6f} "
        f"risk={sizing['risk_amount']:.2f} "
        f"notional={sizing['notional']:.2f} "
        f"lev={sizing['leverage']:.2f}x entry_fee={fee:.2f}")
    journal.log_votes(cycle_id, symbol, vote_dicts)
    pid = journal.log_proposal(cycle_id, proposal, "approved", [])
    reason_text = (_router_prefix(r, SPECIALIST["CLEAN_TREND"])
                   + build_entry_reason(proposal, votes, sizing,
                                        state["equity"]))
    trade_id = journal.open_trade(
        pid, proposal, sizing["qty"], sizing["risk_amount"],
        snap["last_ts"], entry_reason=reason_text,
        tp1_price=tp1_price(proposal), entry_fee=fee)
    journal.log_equity(state["equity"], "trade opened")
    print(f"{symbol} [{source}] {r['label']} -> consensus "
          f"{proposal['direction'].upper()} @ {proposal['entry']} "
          f"-> PAPER TRADE #{trade_id} OPENED")
    return {"symbol": symbol, "action": "trade_opened",
            "trade_id": trade_id, "regime": r["label"]}


def run_cycle(journal, base_dir="."):
    results = []
    candles_by_symbol = {}

    # Fetch first so the exit watcher sees the freshest candles.
    fetched = {}
    sample_notice_shown = False
    for symbol in config.SYMBOLS:
        candles, source = market.get_candles(symbol, config.PRIMARY_INTERVAL)
        htf, _ = market.get_candles(symbol, config.HTF_INTERVAL)
        fetched[symbol] = (candles, htf, source)
        candles_by_symbol[symbol] = candles
        if source == "sample" and not sample_notice_shown:
            sample_notice_shown = True
            print("Note: live market data could not be reached (network "
                  "down or the data service is unavailable), so this cycle "
                  "is using the built-in sample data instead. Sample-data "
                  "results are only for testing the software - they say "
                  "nothing about real markets.")

    closed = watch_exits(journal, candles_by_symbol)
    for c in closed:
        print(f"EXIT {c['symbol']} {c['direction']} @ {c['exit_price']} "
              f"({c['reason']}) PnL {c['pnl']:+.2f}")

    for symbol in config.SYMBOLS:
        candles, htf_candles, source = fetched[symbol]
        price = candles[-1]["close"]
        r = regime.classify_regime(candles)
        if r["label"] == "TREND_EXPANSION":
            results.append(_open_breakout(
                journal, base_dir, symbol, candles, htf_candles,
                source, r))
        elif r["label"] == "CLEAN_TREND":
            results.append(_open_consensus(
                journal, base_dir, symbol, candles, htf_candles,
                source, r))
        else:
            detail = (
                f"regime={r['label']} {r['measures']} | router sat "
                f"out: {r['plain']} - no specialist has a proven "
                f"record in this situation yet, so the correct "
                f"decision is no trade.")
            journal.log_cycle(symbol, config.PRIMARY_INTERVAL, source,
                              price, "regime_no_trade", detail)
            print(f"{symbol} [{source}] price={price:.2f} "
                  f"regime={r['label']} -> sat out (no proven "
                  f"specialist for this situation)")
            results.append({"symbol": symbol,
                            "action": "regime_no_trade",
                            "regime": r["label"]})
    return results
