"""Read-only live signals for the simple, signal-first home page.

One question per market: "what would the platform's own analysis do
right now - BUY, SELL or WAIT - and why?" The answer is computed with
exactly the analysis path the Regime Router bot uses (engine_router):

  1. regime.classify_regime labels the market situation;
  2. TREND_EXPANSION hands the decision to the Breakout specialist
     (breakout.breakout_signal - Donchian-20 fresh cross, fail-closed
     4h trend, volatility guard);
  3. CLEAN_TREND hands it to the v2-gated analyst consensus
     (agents.run_agents -> decision.build_proposal ->
     strategy_v2.filter_proposal);
  4. COMPRESSION / RANGING / MIXED_UNCLEAR have no proven specialist,
     so the answer is WAIT - sitting out is the strategy there.

Nothing here opens a trade, writes a journal, or touches the risk
policy: it is the router's *analysis* half, read-only. Strategy and
risk logic are imported unchanged, never modified.

FAIL CLOSED, always: the data layer falls back to bundled sample data
when live data cannot be fetched. A signal computed on sample data
would be a fabricated signal, so any non-"live" source - for either
timeframe - returns an honest WAIT state with an error explanation
instead of numbers. Sample data is never used here.

Results are cached briefly (a single market for 120 s, the overview
for 180 s) because the screener + per-symbol candle fetches are the
heaviest reads the platform makes, and the home page is the most
refreshed page. Only successful live results are cached.

LIVE PRICE OVERLAY: the signal itself is computed from the last
COMPLETED hourly candle - that is the tested strategy the paper bots
trade, and it must not change. But a card that labels that candle's
close "price now" reads as stale data, so every card served also
carries `live_price`: the symbol's price right now, fetched FRESH
from Binance's public ticker endpoint at response time (never read
from, or written into, the signal cache). The UI shows the live price
prominently and labels the candle close honestly as the signal price
of the last completed hour. If the ticker fetch fails, live_price is
None and the UI shows only the honestly-labelled signal price.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

import config
import data as market
import indicators
import regime
import screener
from agents import run_agents
from breakout import breakout_signal, donchian
from decision import build_proposal
from htf import HTFTrend
from strategy_v2 import filter_proposal

HOUR_MS = 3_600_000

SINGLE_TTL_S = 120
OVERVIEW_TTL_S = 180
OVERVIEW_TOP_N = 10

STRATEGY_BREAKOUT = "Breakout specialist (Donchian-20 channel breakout)"
STRATEGY_CONSENSUS = "Analyst consensus (4 agents, v2 safety filter)"
STRATEGY_NONE = "None - no proven specialist for this situation"
STRATEGY_NO_DATA = "None - live data unavailable"

_cache_lock = threading.Lock()
_single_cache = {}     # symbol -> (fetched_at_s, card)
_overview_cache = None  # (fetched_at_s, payload)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")


def normalize_symbol(raw):
    """'btc', 'BTC/USDT', ' eth ' -> 'BTCUSDT' / 'ETHUSDT'.
    Returns None when the input cannot be a Binance USDT pair."""
    if not raw:
        return None
    sym = re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()
    if not sym:
        return None
    if not sym.endswith("USDT"):
        sym += "USDT"
    if len(sym) > 20 or not _SYMBOL_RE.match(sym):
        return None
    return sym


def _base_card(symbol):
    return {
        "symbol": symbol,
        "action": "WAIT",
        "entry": None,
        "take_profit": None,
        "stop_loss": None,
        "confidence": None,
        "regime": None,
        "regime_plain": None,
        "strategy": STRATEGY_NONE,
        "reasons": [],
        "price": None,
        "live_price": None,
        "as_of": None,
        "source": "live",
    }


def _fail_card(symbol, message):
    card = _base_card(symbol)
    card["strategy"] = STRATEGY_NO_DATA
    card["source"] = "unavailable"
    card["error"] = message
    card["reasons"] = [
        "Live market data could not be fetched for this market, so "
        "no signal is shown. AlphaSwarm never invents prices or "
        "signals - when the data is not really there, the honest "
        "answer is WAIT. Please try again in a few minutes."]
    card["as_of"] = int(time.time() * 1000)
    return card


_V2_PLAIN = {
    "v2_momentum_required": (
        "the Momentum agent did not agree with that direction, and "
        "the v2 safety filter requires momentum agreement before any "
        "trade"),
    "v2_htf_unknown": (
        "the 4-hour (bigger-picture) trend is unclear, and the v2 "
        "safety filter refuses to trade without a clear bigger trend"),
    "v2_htf_filter": (
        "that direction fights the 4-hour (bigger-picture) trend, "
        "and the v2 safety filter only allows trading WITH the "
        "bigger trend"),
}

_BREAKOUT_WAIT_PLAIN = {
    "trend_block": (
        "a breakout move appeared, but the 4-hour (bigger-picture) "
        "trend does not agree with it - or is not clear - and this "
        "specialist only ever trades with the bigger trend"),
    "vol_guard": (
        "a breakout move appeared, but volatility is already in its "
        "top 10% - breakouts that start when the market is already "
        "exhausted usually fail, so this one is skipped"),
    "no_atr": (
        "there is not enough reliable volatility data yet to size a "
        "safe stop-loss, so no signal is given"),
}


def _vote_lines(votes):
    lines = []
    for v in votes:
        ev = v.evidence[0] if v.evidence else ""
        lines.append(
            f"{v.agent}: {v.direction} (confidence "
            f"{v.confidence:.0%})" + (f" - {ev}" if ev else ""))
    return lines


def _signal_for_symbol(symbol):
    """Compute one market's signal from LIVE data. Never raises for
    data problems - those become a fail-closed WAIT card."""
    try:
        candles, source = market.get_candles(
            symbol, config.PRIMARY_INTERVAL)
        htf_candles, htf_source = market.get_candles(
            symbol, config.HTF_INTERVAL)
    except Exception as exc:
        return _fail_card(
            symbol, f"Live data fetch failed ({type(exc).__name__}).")
    if source != "live" or htf_source != "live" or not candles \
            or not htf_candles:
        return _fail_card(
            symbol,
            "Live data is unavailable for this market right now "
            "(the symbol may not exist, or the data service could "
            "not be reached).")

    card = _base_card(symbol)
    card["price"] = candles[-1]["close"]
    card["as_of"] = candles[-1]["ts"]
    r = regime.classify_regime(candles)
    card["regime"] = r["label"]
    card["regime_plain"] = r["plain"]

    if r["er"] is None or r["adx"] is None or r["bbw_pct"] is None:
        card["reasons"] = [
            f"Market situation: {r['plain']}",
            "There is not enough price history yet to judge this "
            "market reliably, so the honest answer is WAIT."]
        return card

    situation = (f"Market situation: {r['label'].replace('_', ' ')} - "
                 f"{r['plain']} (efficiency ratio {r['er']}, trend "
                 f"strength ADX {r['adx']}, volatility percentile "
                 f"{r['bbw_pct']}).")

    if r["label"] == "TREND_EXPANSION":
        card["strategy"] = STRATEGY_BREAKOUT
        atr_values = indicators.atr_series(candles, 14)
        trend = HTFTrend(htf_candles).at(candles[-1]["ts"] + HOUR_MS)
        raw, why = breakout_signal(candles, len(candles) - 1,
                                   atr_values, trend)
        if raw is None:
            if why == "inside":
                hi, lo = donchian(candles, len(candles) - 1)
                card["reasons"] = [
                    situation,
                    f"The breakout specialist is watching, but the "
                    f"price (${card['price']:,.4f}) is still INSIDE "
                    f"its last-20-hours range (${lo:,.4f} - "
                    f"${hi:,.4f}). No breakout has happened yet, so "
                    f"the answer is WAIT."]
            else:
                card["reasons"] = [
                    situation,
                    "The breakout specialist says WAIT: "
                    + _BREAKOUT_WAIT_PLAIN.get(
                        why, "its entry rules are not met right now.")
                    + "."]
            return card
        card["action"] = "BUY" if raw["direction"] == "long" else "SELL"
        card["entry"] = raw["entry"]
        card["take_profit"] = raw["take_profit"]
        card["stop_loss"] = raw["stop_loss"]
        # A breakout is a rule event, not a vote: there is no honest
        # confidence number, so confidence stays null on purpose.
        level = "highest high" if raw["direction"] == "long" \
            else "lowest low"
        edge = raw["channel_high"] if raw["direction"] == "long" \
            else raw["channel_low"]
        card["reasons"] = [
            situation,
            f"Breakout: the price closed at ${raw['entry']:,.4f}, "
            f"outside the {level} of the last 20 hours "
            f"(${edge:,.4f}) - the coin has left the range it was "
            f"stuck in, which can mark the start of a new move.",
            f"The 4-hour trend agrees ({trend}), and volatility is "
            f"busy but not exhausted (at the {raw['atr_pct']:.0%} "
            f"mark of its recent range; the top 10% is skipped).",
            "Breakout signals are rule events, not agent votes, so "
            "no vote confidence is shown for this one.",
            "If a paper bot took this trade, its stop-loss and "
            "take-profit would be the levels shown - set before "
            "entry, never moved by hope."]
        return card

    if r["label"] == "CLEAN_TREND":
        card["strategy"] = STRATEGY_CONSENSUS
        snap = indicators.compute_snapshot(candles, htf_candles)
        votes = run_agents(snap)
        proposal = build_proposal(symbol, snap, votes)
        if proposal is None:
            tally = {}
            for v in votes:
                tally[v.direction] = tally.get(v.direction, 0) + 1
            card["reasons"] = [
                situation,
                "The analyst agents did not reach a tradeable "
                f"consensus (votes: "
                + ", ".join(f"{k} {v}" for k, v in
                            sorted(tally.items()))
                + "). A trade needs at least 3 of the 4 agents "
                "agreeing on one direction with an average "
                "confidence of at least 60% - so the answer is "
                "WAIT.",
            ] + _vote_lines(votes)
            return card
        ok_v2, reason_v2 = filter_proposal(proposal, snap, votes)
        direction_word = proposal["direction"].upper()
        if not ok_v2:
            card["reasons"] = [
                situation,
                f"The analyst agents leaned {direction_word} "
                f"(average confidence "
                f"{proposal['avg_confidence']:.0%}), but the v2 "
                f"safety filter blocked it: "
                + _V2_PLAIN.get(reason_v2, reason_v2) + ".",
            ] + _vote_lines(votes)
            return card
        agreeing = [v for v in votes
                    if v.direction == proposal["direction"]]
        card["action"] = "BUY" if proposal["direction"] == "long" \
            else "SELL"
        card["entry"] = proposal["entry"]
        card["take_profit"] = proposal["take_profit"]
        card["stop_loss"] = proposal["stop_loss"]
        card["confidence"] = proposal["avg_confidence"]
        card["reasons"] = [
            situation,
            f"{len(agreeing)} of {len(votes)} analyst agents agreed "
            f"on a {direction_word} (average confidence "
            f"{proposal['avg_confidence']:.0%}), and the v2 safety "
            f"filter passed it: the Momentum agent agrees and the "
            f"trade goes WITH the 4-hour trend.",
        ] + _vote_lines(votes) + [
            "The stop-loss and take-profit shown are set before "
            "entry by the same rules the paper bots trade with."]
        return card

    # COMPRESSION / RANGING / MIXED_UNCLEAR: no proven specialist.
    card["reasons"] = [
        situation,
        "No specialist strategy has a proven record in this kind of "
        "market, so AlphaSwarm sits out rather than forcing a trade. "
        "Waiting is the decision here - most trading losses happen "
        "when people trade markets they have no edge in."]
    return card


def fetch_live_price(symbol):
    """The symbol's price RIGHT NOW, from Binance's public ticker
    endpoint (no API key, read-only). Tries each configured public
    host in turn. Returns a float, or None on any failure - this
    function never raises, because a missing live overlay must never
    break the signal itself."""
    for host in config.BINANCE_HOSTS:
        try:
            url = f"{host}/api/v3/ticker/price?symbol={symbol}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "AlphaSwarm/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return float(payload["price"])
        except Exception:
            continue
    return None


def _with_live_price(card):
    """A shallow copy of `card` with `live_price` overlaid FRESH.

    get_signal() hands out the cached dict object itself, so the
    overlay is always applied to a copy: the cache is never mutated,
    and the cached card never carries a price that quietly goes
    stale. Fail-closed cards (source != "live") get live_price=None -
    no invented numbers next to an honest error."""
    view = dict(card)
    view["live_price"] = None
    if card.get("source") == "live":
        view["live_price"] = fetch_live_price(card["symbol"])
    return view


def get_signal(symbol):
    """One market's current signal card (cached briefly), with a
    fresh live-price overlay applied at response time."""
    now = time.time()
    with _cache_lock:
        hit = _single_cache.get(symbol)
        if hit and now - hit[0] < SINGLE_TTL_S:
            return _with_live_price(hit[1])
    card = _signal_for_symbol(symbol)
    if card.get("source") == "live":
        with _cache_lock:
            _single_cache[symbol] = (now, card)
    return _with_live_price(card)


def get_signals(top_n=OVERVIEW_TOP_N):
    """Overview cards for the top screener markets. Fail closed: if
    the screener cannot fetch live data, returns an empty list plus
    an honest error - never stale or invented cards."""
    global _overview_cache
    now = time.time()
    with _cache_lock:
        if _overview_cache and now - _overview_cache[0] < OVERVIEW_TTL_S:
            cached = _overview_cache[1]
            return {**cached,
                    "signals": [_with_live_price(c)
                                for c in cached["signals"]]}
    try:
        result = screener.run_screener(top_n=top_n, write=False)
        uni = screener.build_trading_universe(top_n=top_n,
                                              result=result)
    except Exception as exc:
        return {"signals": [],
                "error": "The market screener could not fetch live "
                         f"data ({type(exc).__name__}), so no "
                         "signals are shown rather than invented "
                         "ones. Please try again in a few minutes.",
                "as_of": int(time.time() * 1000)}
    cards = []
    for symbol in uni["symbols"][:top_n]:
        cards.append(get_signal(symbol))
    payload = {"signals": cards, "error": None,
               "as_of": int(time.time() * 1000),
               "universe_note": (
                   f"Top {len(cards)} markets by the platform's "
                   f"published screener score (liquidity + activity), "
                   f"from {uni['eligible_count']} eligible coins; "
                   "tokenized stocks and gold tokens are excluded.")}
    with _cache_lock:
        _overview_cache = (now, payload)
    return {**payload,
            "signals": [_with_live_price(c) for c in payload["signals"]]}
