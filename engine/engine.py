"""One analysis cycle: fetch data -> watch exits -> agents vote ->
consensus proposal -> risk policy gate -> simulated fill.

Platform copy: identical decision logic to the original AlphaPaper
engine, plus (a) a plain-language reason stored on every opened trade
(the Reporting Agent's job in the blueprint), (b) the TP1 level and
entry fee passed to the journal for the platform execution upgrades.
"""

import time

import config
import data as market
import indicators
import policy
from agents import run_agents
from decision import build_proposal
from execution import entry_fee, watch_exits


def build_entry_reason(proposal, votes, sizing, equity):
    """Plain-language explanation of why a trade was opened, written for
    a non-technical reader. Built only from the actual votes and the
    actual risk numbers - nothing invented."""
    agreeing = [v for v in votes if v.direction == proposal["direction"]]
    lines = [
        f"Why this trade: {len(agreeing)} of {len(votes)} analyst agents "
        f"agreed on a {proposal['direction'].upper()} "
        f"(average confidence {proposal['avg_confidence']:.0%})."
    ]
    for v in votes:
        ev = "; ".join(v.evidence[:2])
        lines.append(f"- {v.agent}: {v.direction} "
                     f"(confidence {v.confidence:.0%}) - {ev}")
    risk_dist = abs(proposal["entry"] - proposal["stop_loss"])
    tp1 = proposal["entry"] + risk_dist if proposal["direction"] == "long" \
        else proposal["entry"] - risk_dist
    lines.append(
        f"Risk Manager approved it: risking ${sizing['risk_amount']:.2f} "
        f"({config.RISK_PER_TRADE:.0%} of the ${equity:,.2f} paper "
        f"balance). Stop loss ${proposal['stop_loss']:.4f}, first target "
        f"${tp1:.4f} (half the position is booked there and the stop "
        f"moves to break-even), final target "
        f"${proposal['take_profit']:.4f}. Paper trade - no real money.")
    return "\n".join(lines)


def tp1_price(proposal):
    risk_dist = abs(proposal["entry"] - proposal["stop_loss"])
    if proposal["direction"] == "long":
        return round(proposal["entry"] + config.TP1_R_MULTIPLE * risk_dist, 4)
    return round(proposal["entry"] - config.TP1_R_MULTIPLE * risk_dist, 4)


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
        candles, htf, source = fetched[symbol]
        snap = indicators.compute_snapshot(candles, htf)
        votes = run_agents(snap)
        vote_dicts = [v.to_dict() for v in votes]
        proposal = build_proposal(symbol, snap, votes)

        if proposal is None:
            tally = {}
            for v in votes:
                tally[v.direction] = tally.get(v.direction, 0) + 1
            cycle_id = journal.log_cycle(
                symbol, config.PRIMARY_INTERVAL, source, snap["price"],
                "no_proposal", f"vote tally {tally}")
            journal.log_votes(cycle_id, symbol, vote_dicts)
            print(f"{symbol} [{source}] price={snap['price']:.2f} "
                  f"votes={tally} -> no proposal")
            results.append({"symbol": symbol, "action": "no_proposal"})
            continue

        if config.STRATEGY == "v2":
            from strategy_v2 import filter_proposal
            ok_v2, reason_v2 = filter_proposal(proposal, snap, votes)
            if not ok_v2:
                cycle_id = journal.log_cycle(
                    symbol, config.PRIMARY_INTERVAL, source, snap["price"],
                    "proposal_rejected", f"strategy v2 filter: {reason_v2}")
                journal.log_votes(cycle_id, symbol, vote_dicts)
                journal.log_proposal(cycle_id, proposal, "rejected",
                                     [f"strategy v2 filter: {reason_v2}"])
                print(f"{symbol} [{source}] proposal BLOCKED by strategy "
                      f"v2 filter: {reason_v2}")
                results.append({"symbol": symbol,
                                "action": "proposal_rejected",
                                "reasons": [reason_v2]})
                continue

        state = journal.daily_state()
        approved, reasons, sizing = policy.check_proposal(
            proposal, state, snap["last_ts"], config.PRIMARY_INTERVAL,
            base_dir=base_dir)
        if approved:
            cycle_id = journal.log_cycle(
                symbol, config.PRIMARY_INTERVAL, source, snap["price"],
                "trade_opened",
                f"qty={sizing['qty']:.6f} risk={sizing['risk_amount']:.2f} "
                f"notional={sizing['notional']:.2f} "
                f"lev={sizing['leverage']:.2f}x")
            journal.log_votes(cycle_id, symbol, vote_dicts)
            pid = journal.log_proposal(cycle_id, proposal, "approved", [])
            reason_text = build_entry_reason(proposal, votes, sizing,
                                             state["equity"])
            fee = entry_fee(proposal["entry"], sizing["qty"])
            trade_id = journal.open_trade(
                pid, proposal, sizing["qty"], sizing["risk_amount"],
                snap["last_ts"], entry_reason=reason_text,
                tp1_price=tp1_price(proposal), entry_fee=round(fee, 2))
            journal.log_equity(state["equity"], "trade opened")
            print(f"{symbol} [{source}] {proposal['direction'].upper()} "
                  f"@ {proposal['entry']} SL {proposal['stop_loss']} "
                  f"TP {proposal['take_profit']} qty={sizing['qty']:.6f} "
                  f"-> PAPER TRADE #{trade_id} OPENED")
            results.append({"symbol": symbol, "action": "trade_opened",
                            "trade_id": trade_id})
        else:
            cycle_id = journal.log_cycle(
                symbol, config.PRIMARY_INTERVAL, source, snap["price"],
                "proposal_rejected", "; ".join(reasons))
            journal.log_votes(cycle_id, symbol, vote_dicts)
            journal.log_proposal(cycle_id, proposal, "rejected", reasons)
            print(f"{symbol} [{source}] proposal REJECTED by policy: "
                  f"{'; '.join(reasons)}")
            results.append({"symbol": symbol, "action": "proposal_rejected",
                            "reasons": reasons})
    return results
