"""One Range Specialist (candidate) cycle: fetch data -> watch exits
-> range-fade self-gate + stochastic-turn signal -> risk policy gate
-> simulated fill.

The signal source is range_fade.range_signal (see that module and
~/workspace/alpha-paper-research/range2/DESIGN.md for the frozen
design): the specialist carries its OWN ranging gate - ADX(14) < 20,
a flat EMA50 slope, and an established 48-bar channel at least
3xATR wide - and refuses to trade unless all three hold. When the
gate is closed the cycle journals a "range_no_trade" sit-out with
the measures behind it, exactly like the Regime Router journals its
sit-outs; when the gate is open but no stochastic turn has fired,
that is journaled too.

CANDIDATE STATUS: this strategy has NOT passed the project's strict
bar and has no backtest - by design it is judged on forward paper
data only, starting from its first live cycle. It is not part of
the Regime Router's admitted roster.

Execution is the platform's, unchanged: the deterministic risk
policy (1% risk, max 2 open, -3% daily halt, mandatory SL/TP,
stale-data refusal, kill switch), 0.1%-per-side paper fees, and
the execution upgrades (TP1 partial booking at 1R, break-even stop
move, trailing stop) via execution.watch_exits. Live-fill note
(same as the other platform engines): a live runner fills at the
signal bar's close, the reference price the proposal was built
around.
"""

import config
import data as market
import indicators
import policy
from engine import tp1_price
from execution import entry_fee, watch_exits
from range_fade import build_series, range_measures, range_signal

GATE_TEXT = {
    "gate_trend": "trend strength ADX is at or above 20 - Wilder's "
                  "line for a real trend - so this is not a sideways "
                  "market",
    "gate_slope": "the 50-hour trend line is not flat - it drifted "
                  "more than half an average hourly range in the "
                  "last day, so price is going somewhere",
    "gate_narrow": "the 48-hour range is narrower than 3x the "
                   "average hourly range - a coil that tight is "
                   "compression, not a range worth fading",
    "warmup": "not enough price history yet to judge the market",
    "no_atr": "volatility (ATR) is not available yet",
}


def _measures_text(m):
    if m["atr"] is None:
        return "measures unavailable"
    parts = []
    if m["adx"] is not None:
        parts.append(f"adx={m['adx']:.1f}")
    if m["slope_atr"] is not None:
        parts.append(f"ema50_slope={m['slope_atr']:.2f}atr")
    if m["width_atr"] is not None:
        parts.append(f"range_width={m['width_atr']:.1f}atr "
                     f"[{m['channel_low']:.2f}, {m['channel_high']:.2f}]")
    if m["stoch_k"] is not None:
        parts.append(f"stoch_k={m['stoch_k']:.1f} "
                     f"(prev {m['stoch_k_prev']:.1f})")
    return " ".join(parts)


def build_range_reason(raw, sizing, equity):
    """Plain-language explanation of why a range-fade trade was
    opened, written for a non-technical reader. Built only from the
    actual gate/signal numbers and the actual risk numbers -
    nothing invented. States the candidate status plainly."""
    direction = raw["direction"]
    if direction == "long":
        turn = (f"momentum turned UP out of the oversold zone: the "
                f"stochastic oscillator rose from {raw['stoch_k_prev']} "
                f"to {raw['stoch_k']}, crossing back above 20 - this "
                f"bot buys the turn, not the extreme itself")
        target_word = "TOP"
    else:
        turn = (f"momentum turned DOWN out of the overbought zone: "
                f"the stochastic oscillator fell from "
                f"{raw['stoch_k_prev']} to {raw['stoch_k']}, crossing "
                f"back below 80 - this bot sells the turn, not the "
                f"extreme itself")
        target_word = "BOTTOM"
    lines = [
        f"Why this trade: the market for this coin is moving "
        f"SIDEWAYS, and all three of this bot's sideways tests pass: "
        f"trend strength ADX is {raw['adx']} (below 20 means no real "
        f"trend), the 50-hour trend line drifted only "
        f"{raw['slope_atr']}x an average hourly range in the last "
        f"day (flat), and price has been contained in a range "
        f"${raw['channel_low']:,.4f} - ${raw['channel_high']:,.4f} "
        f"for two days. In that situation, {turn}.",
        f"- The target is the {target_word} of the range "
        f"(${raw['take_profit']:,.4f}) - the trade's idea is that "
        f"price crosses the range it has been stuck in.",
        f"Risk Manager approved it: risking "
        f"${sizing['risk_amount']:.2f} ({config.RISK_PER_TRADE:.0%} of "
        f"the ${equity:,.2f} paper balance). Stop loss "
        f"${raw['stop_loss']:,.4f} (1.5x the average hourly range), "
        f"first target ${tp1_price(raw):,.4f} (half the position is "
        f"booked there and the stop moves to break-even), final "
        f"target ${raw['take_profit']:,.4f}.",
        f"CANDIDATE STRATEGY - NOT YET PROVEN: this bot is a "
        f"research candidate being judged on its forward paper "
        f"record, which starts now. It has no backtest result and "
        f"no track record. Paper trade - no real money.",
    ]
    return "\n".join(lines)


def run_cycle(journal, base_dir="."):
    results = []
    candles_by_symbol = {}

    # Fetch first so the exit watcher sees the freshest candles.
    fetched = {}
    sample_notice_shown = False
    for symbol in config.SYMBOLS:
        candles, source = market.get_candles(symbol, config.PRIMARY_INTERVAL)
        fetched[symbol] = (candles, source)
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
        candles, source = fetched[symbol]
        price = candles[-1]["close"]
        signal_ts = candles[-1]["ts"]
        i = len(candles) - 1

        atr_values = indicators.atr_series(candles, 14)
        series = build_series(candles)
        raw, reason = range_signal(candles, i, atr_values, series)

        if raw is None:
            m = range_measures(candles, i, atr_values, series)
            measures = _measures_text(m)
            if reason.startswith("gate_") or reason in ("warmup",
                                                        "no_atr"):
                action = "range_no_trade"
                detail = (f"range gate CLOSED ({reason}): "
                          f"{GATE_TEXT[reason]} | {measures} | "
                          f"specialist sat out - it only trades a "
                          f"demonstrably sideways market")
                print(f"{symbol} [{source}] price={price:.2f} "
                      f"range gate closed ({reason}) -> sat out")
            elif reason == "target_too_close":
                action = "no_proposal"
                detail = (f"range gate OPEN, stochastic turn fired, "
                          f"but the far side of the range is less "
                          f"than 1.5x the stop distance away - not "
                          f"enough room for the risk | {measures}")
                print(f"{symbol} [{source}] price={price:.2f} "
                      f"turn fired but target too close -> no proposal")
            else:  # no_cross
                action = "no_proposal"
                detail = (f"range gate OPEN (market is sideways) "
                          f"but no stochastic turn out of an extreme "
                          f"zone on this bar | {measures}")
                print(f"{symbol} [{source}] price={price:.2f} "
                      f"gate open, no turn -> no proposal")
            journal.log_cycle(symbol, config.PRIMARY_INTERVAL, source,
                              price, action, detail)
            results.append({"symbol": symbol, "action": action,
                            "reason": reason})
            continue

        proposal = {"symbol": symbol, "direction": raw["direction"],
                    "entry": raw["entry"], "stop_loss": raw["stop_loss"],
                    "take_profit": raw["take_profit"],
                    "avg_confidence": None}
        detail = (f"range fade {raw['direction']} | "
                  f"{_measures_text(range_measures(candles, i, atr_values, series))} "
                  f"atr={raw['atr']:.4f}")

        # One trade per signal bar: if this bar already produced a
        # trade, a re-run inside the same hour must not duplicate it.
        if journal.trade_exists_for_signal(symbol, signal_ts):
            journal.log_cycle(symbol, config.PRIMARY_INTERVAL, source,
                              price, "no_proposal",
                              detail + " | already acted on this signal bar")
            print(f"{symbol} [{source}] range fade {raw['direction']} "
                  f"already acted on for this signal bar -> skipped")
            results.append({"symbol": symbol, "action": "no_proposal"})
            continue

        state = journal.daily_state()
        approved, reasons, sizing = policy.check_proposal(
            proposal, state, signal_ts, config.PRIMARY_INTERVAL,
            base_dir=base_dir)
        if approved:
            fee = round(entry_fee(proposal["entry"], sizing["qty"]), 2)
            cycle_id = journal.log_cycle(
                symbol, config.PRIMARY_INTERVAL, source, price,
                "trade_opened",
                f"{detail} | qty={sizing['qty']:.6f} "
                f"risk={sizing['risk_amount']:.2f} "
                f"notional={sizing['notional']:.2f} "
                f"lev={sizing['leverage']:.2f}x entry_fee={fee:.2f}")
            pid = journal.log_proposal(cycle_id, proposal, "approved", [])
            reason_text = build_range_reason(raw, sizing,
                                             state["equity"])
            trade_id = journal.open_trade(
                pid, proposal, sizing["qty"], sizing["risk_amount"],
                signal_ts, entry_reason=reason_text,
                tp1_price=tp1_price(proposal), entry_fee=fee)
            journal.log_equity(state["equity"], "trade opened")
            print(f"{symbol} [{source}] {proposal['direction'].upper()} "
                  f"@ {proposal['entry']} SL {proposal['stop_loss']} "
                  f"TP {proposal['take_profit']} qty={sizing['qty']:.6f} "
                  f"entry_fee={fee:.2f} "
                  f"-> PAPER TRADE #{trade_id} OPENED")
            results.append({"symbol": symbol, "action": "trade_opened",
                            "trade_id": trade_id})
        else:
            cycle_id = journal.log_cycle(
                symbol, config.PRIMARY_INTERVAL, source, price,
                "proposal_rejected", f"{detail} | " + "; ".join(reasons))
            journal.log_proposal(cycle_id, proposal, "rejected", reasons)
            print(f"{symbol} [{source}] proposal REJECTED by policy: "
                  f"{'; '.join(reasons)}")
            results.append({"symbol": symbol,
                            "action": "proposal_rejected",
                            "reasons": reasons})
    return results
