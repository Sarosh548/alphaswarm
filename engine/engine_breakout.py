"""One breakout analysis cycle: fetch data -> watch exits -> breakout
signal -> risk policy gate -> simulated fill.

This is the platform's engine with ONE substitution, exactly as in the
proven forward breakout runner: the proposal source. Instead of the
four analyst agents and their consensus, each symbol's proposal comes
from breakout.breakout_signal evaluated on the latest CLOSED 1h
candle: a Donchian-20 fresh-cross event, kept only when the fail-closed
4h trend (htf.HTFTrend, the backtest's own labels) agrees and the ATR
top-decile volatility guard passes.

Platform execution differences vs the pure study (stated compromise,
see RESULTS_HARDENING.md): the study's entry geometry is kept exactly
(stop = 1.5 x ATR14, final target = 2R, 0.1% fee per side), but exits
run through the platform execution engine, so the TP1 partial booking
at 1R (50%), the break-even stop move and the trailing stop also apply
to breakout trades, as they do for every other platform bot. The pure
study record continues to live in the frozen forward runner
(~/workspace/alpha-paper-forward/breakout/); this bot is the same
entries under platform execution.

Live-fill note (same as the forward runner): the backtest fills a
breakout at the NEXT bar's open and shifts SL/TP to that fill. A live
runner has no next bar yet, so it fills at the signal bar's close, the
reference price the proposal was built around.
"""

import config
import data as market
import indicators
import policy
from breakout import breakout_signal
from engine import tp1_price
from execution import entry_fee, watch_exits
from htf import HTFTrend

HOUR_MS = 3_600_000


def build_breakout_reason(raw, trend, sizing, equity):
    """Plain-language explanation of why a breakout trade was opened,
    written for a non-technical reader. Built only from the actual
    signal numbers and the actual risk numbers - nothing invented."""
    direction = raw["direction"]
    if direction == "long":
        move = (f"the price closed at ${raw['entry']:,.4f}, ABOVE the "
                f"highest high of the last 20 hours "
                f"(${raw['channel_high']:,.4f})")
        trend_word = "UP"
    else:
        move = (f"the price closed at ${raw['entry']:,.4f}, BELOW the "
                f"lowest low of the last 20 hours "
                f"(${raw['channel_low']:,.4f})")
        trend_word = "DOWN"
    lines = [
        f"Why this trade: {move} - a Donchian breakout, the moment a "
        f"coin leaves the range it was stuck in, which can mark the "
        f"start of a new move. The 4-hour trend is {trend_word}, and "
        f"this bot only ever trades WITH the bigger trend (a breakout "
        f"against it, or with no clear trend, is skipped).",
        f"- Volatility check passed: current volatility sits at the "
        f"{raw['atr_pct']:.0%} mark of its recent range - busy, but "
        f"not in the exhausted top 10% where breakouts usually fail.",
        f"Risk Manager approved it: risking "
        f"${sizing['risk_amount']:.2f} ({config.RISK_PER_TRADE:.0%} of "
        f"the ${equity:,.2f} paper balance). Stop loss "
        f"${raw['stop_loss']:,.4f} (1.5x the average hourly range), "
        f"first target ${tp1_price(raw):,.4f} (half the position is "
        f"booked there and the stop moves to break-even), final "
        f"target ${raw['take_profit']:,.4f}. Paper trade - no real "
        f"money.",
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
        signal_ts = candles[-1]["ts"]

        atr_values = indicators.atr_series(candles, 14)
        trend = HTFTrend(htf_candles).at(signal_ts + HOUR_MS)
        raw, reason = breakout_signal(candles, len(candles) - 1,
                                      atr_values, trend)

        if raw is None:
            detail = f"breakout={reason} 4h_trend={trend}"
            journal.log_cycle(symbol, config.PRIMARY_INTERVAL, source,
                              price, "no_proposal", detail)
            print(f"{symbol} [{source}] price={price:.2f} "
                  f"breakout={reason} trend={trend} -> no proposal")
            results.append({"symbol": symbol, "action": "no_proposal"})
            continue

        proposal = {"symbol": symbol, "direction": raw["direction"],
                    "entry": raw["entry"], "stop_loss": raw["stop_loss"],
                    "take_profit": raw["take_profit"],
                    "avg_confidence": None}
        detail = (f"breakout {raw['direction']} channel "
                  f"[{raw['channel_low']:.2f}, {raw['channel_high']:.2f}] "
                  f"atr={raw['atr']:.4f} atr_pct={raw['atr_pct']} "
                  f"4h_trend={trend}")

        # One trade per signal bar: if this bar already produced a
        # trade, a re-run inside the same hour must not duplicate it.
        if journal.trade_exists_for_signal(symbol, signal_ts):
            journal.log_cycle(symbol, config.PRIMARY_INTERVAL, source,
                              price, "no_proposal",
                              detail + " | already acted on this signal bar")
            print(f"{symbol} [{source}] breakout {raw['direction']} "
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
            reason_text = build_breakout_reason(raw, trend, sizing,
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
