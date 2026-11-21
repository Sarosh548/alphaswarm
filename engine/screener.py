"""Stage A (v3) - all-coin screener.

Pulls the FULL Binance USDT spot universe from Binance's PUBLIC data host
(no API key, read-only), applies published eligibility filters, ranks the
survivors with a transparent score, and writes the top N to a report.

Filters (all in config.py, all applied in this order):
  F1  pair is a spot USDT pair with status TRADING
  F2  base asset is not a fiat/stablecoin base (USDC, EUR, DAI, ...)
  F3  not a leveraged token (BTCUP, ETHDOWN, ...): base ends in
      UP/DOWN/BULL/BEAR *and* the stripped prefix is itself a listed base
      asset - this keeps real coins like JUP (prefix "J" is not an asset)
  F4  24h quote volume >= config.SCREENER_MIN_QUOTE_VOLUME
  F5  |24h price change| <= config.SCREENER_MAX_ABS_CHANGE_PCT (sanity:
      excludes broken/illiquid prints and post-crash debris)

Score (transparent, min-max normalised across the survivors, 0..1 each):
  score = 0.50 * norm(log10(24h quote volume))
        + 0.25 * norm(|24h % change|)          (volatility)
        + 0.25 * norm(24h % change)            (momentum, signed)

Outputs: screener_report.md (human), screener_universe.json (machine:
the eligible universe + top list; events.py uses it as its symbol
dictionary). Run:  python3 screener.py
"""

import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request

import config

FIAT_OR_STABLE_BASES = {
    # stablecoins
    "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "GUSD", "USDJ",
    "XUSD", "USD1", "RLUSD", "BFUSD", "EUR", "GBP",
    # fiat bases quoted against USDT on Binance spot
    "BRL", "TRY", "ZAR", "AUD", "JPY", "MXN", "ARS", "NGN", "RUB",
    "UAH", "BIDR", "IDRT", "VAI", "PLN", "RON", "CZK", "HUF",
}
_LEVERAGED_SUFFIXES = ("BULL", "BEAR", "UP", "DOWN")


def _get_json(url, timeout=25):
    req = urllib.request.Request(
        url, headers={"User-Agent": "AlphaPaperV3-Screener/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_universe():
    """Return (meta, tickers) from the first reachable public host.
    meta: {pair: {"base": baseAsset}} for spot USDT pairs, any status.
    tickers: {pair: raw 24h ticker dict}. Raises on total failure."""
    last_err = None
    for host in config.BINANCE_HOSTS:
        try:
            info = _get_json(f"{host}/api/v3/exchangeInfo")
            tick = _get_json(f"{host}/api/v3/ticker/24hr")
            meta = {}
            for s in info.get("symbols", []):
                if s.get("quoteAsset") == "USDT" and s.get("isSpotTradingAllowed", True):
                    meta[s["symbol"]] = {"base": s["baseAsset"],
                                         "status": s.get("status", "")}
            tickers = {t["symbol"]: t for t in tick
                       if t["symbol"] in meta}
            return meta, tickers, host
        except (urllib.error.URLError, ValueError, OSError, TimeoutError,
                KeyError, http.client.HTTPException) as exc:
            # HTTPException covers IncompleteRead (connection cut
            # mid-body on the ~18MB exchangeInfo payload): a fetch
            # failure like any other - try the next host.
            last_err = exc
            continue
    raise RuntimeError(f"could not reach any Binance public host: {last_err}")


def is_leveraged_token(base, all_bases):
    for suf in _LEVERAGED_SUFFIXES:
        if base.endswith(suf) and len(base) > len(suf):
            if base[:-len(suf)] in all_bases:
                return True
    return False


def apply_filters(meta, tickers):
    """Return (survivors, stage_counts). Each survivor is a plain dict."""
    all_bases = {m["base"] for m in meta.values()}
    stages = {"universe_usdt_spot": len(meta)}
    rows = []
    for pair, m in meta.items():
        if m["status"] != "TRADING":
            continue
        rows.append({"symbol": pair, "base": m["base"]})
    stages["F1_trading"] = len(rows)

    rows = [r for r in rows if r["base"] not in FIAT_OR_STABLE_BASES]
    stages["F2_no_fiat_or_stable_base"] = len(rows)

    rows = [r for r in rows if not is_leveraged_token(r["base"], all_bases)]
    stages["F3_no_leveraged_tokens"] = len(rows)

    enriched = []
    for r in rows:
        t = tickers.get(r["symbol"])
        if t is None:
            continue
        try:
            qv = float(t["quoteVolume"])
            chg = float(t["priceChangePercent"])
            last = float(t["lastPrice"])
            vol_base = float(t["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        enriched.append({**r, "last_price": last, "change_pct": chg,
                         "quote_volume": qv, "base_volume": vol_base})
    rows = enriched
    stages["with_ticker"] = len(rows)

    rows = [r for r in rows
            if r["quote_volume"] >= config.SCREENER_MIN_QUOTE_VOLUME]
    stages["F4_min_quote_volume"] = len(rows)

    rows = [r for r in rows
            if abs(r["change_pct"]) <= config.SCREENER_MAX_ABS_CHANGE_PCT]
    stages["F5_move_sanity"] = len(rows)
    return rows, stages


def _norm(values):
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def score_and_rank(rows):
    """Attach score components + total score; return rows ranked desc."""
    if not rows:
        return []
    lv = _norm([math.log10(max(r["quote_volume"], 1.0)) for r in rows])
    vol = _norm([abs(r["change_pct"]) for r in rows])
    mom = _norm([r["change_pct"] for r in rows])
    for i, r in enumerate(rows):
        r["score_volume"] = round(lv[i], 4)
        r["score_volatility"] = round(vol[i], 4)
        r["score_momentum"] = round(mom[i], 4)
        r["score"] = round(0.50 * lv[i] + 0.25 * vol[i] + 0.25 * mom[i], 4)
    rows.sort(key=lambda r: (-r["score"], -r["quote_volume"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def run_screener(top_n=None, write=True):
    top_n = top_n or config.SCREENER_TOP_N
    meta, tickers, host = fetch_universe()
    survivors, stages = apply_filters(meta, tickers)
    ranked = score_and_rank(survivors)
    top = ranked[:top_n]
    result = {"fetched_at_ms": int(time.time() * 1000), "host": host,
              "stages": stages, "eligible": ranked, "top": top}
    if write:
        write_outputs(result)
    return result


def write_outputs(result):
    stamp = time.strftime("%Y-%m-%d %H:%M UTC",
                          time.gmtime(result["fetched_at_ms"] / 1000))
    lines = [
        "# AlphaPaper v3 - Screener report (Stage A)",
        "",
        f"Generated: {stamp} from Binance public data ({result['host']}).",
        "Paper-research only. Filters and score are the published ones in "
        "config.py / screener.py - nothing is hand-picked.",
        "",
        "## Filter funnel",
        "",
    ]
    for k, v in result["stages"].items():
        lines.append(f"- {k}: {v}")
    lines += ["", f"## Top {len(result['top'])} candidates", "",
              "| # | Pair | Last price | 24h % | 24h quote vol (USDT) | "
              "Score | vol | volat | mom |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in result["top"]:
        lines.append(
            f"| {r['rank']} | {r['symbol']} | {r['last_price']:g} | "
            f"{r['change_pct']:+.2f} | {r['quote_volume']:,.0f} | "
            f"{r['score']:.3f} | {r['score_volume']:.2f} | "
            f"{r['score_volatility']:.2f} | {r['score_momentum']:.2f} |")
    with open(config.SCREENER_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    universe = {r["base"]: r["symbol"] for r in result["eligible"]}
    with open(config.SCREENER_UNIVERSE_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetched_at_ms": result["fetched_at_ms"],
                   "universe": universe, "top": result["top"]},
                  f, indent=1)


# ---------------------------------------------------------------------------
# Trading universe (technical bots, universe mode = "screener")
#
# The screener above ranks everything Binance lists against USDT -
# including instruments that are not crypto at all. Those stay in the
# screener's own report (they are real market data, and the Event
# Watcher reports them), but they must never be TRADED by the technical
# bots:
#   (a) XAUT / PAXG - tokenized gold, not a crypto asset.
#   (b) Tokenized stocks ("bStocks"): Binance lists them as the US
#       stock ticker plus a trailing "B" (NVDAB = NVIDIA, TSLAB =
#       Tesla, SOXLB = the SOXL ETF, SPCXB = SpaceX). Rule: the base
#       ends in "B" AND the prefix is a known US stock ticker
#       (BSTOCK_TICKERS) - so a real coin that merely ends in "B" is
#       NOT excluded (ARB -> "AR" is not a stock ticker; JUP, BB are
#       likewise untouched).
# ---------------------------------------------------------------------------

MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

NON_CRYPTO_BASES = {"XAUT", "PAXG"}  # gold tokens - not crypto assets

BSTOCK_TICKERS = {
    # Observed live on Binance (2026-11-19 scan of every eligible
    # USDT base ending in "B"): NVDA, SOXL, SPCX, MSTR, GOOGL, QQQ
    # plus CRCL (Circle), MU (Micron), SOXS, SKHY (SK hynix), INTC,
    # KORU, SNDK, SNXX, TQQQ - the last nine were caught by the first
    # live all-coins cycle AFTER it ran, and added here. A static list
    # lags new bStock listings by construction; extend it as new ones
    # appear. Real coins are safe: BNB ("BN") and YB ("Y") are not
    # stock tickers, so both stay tradeable.
    "AAPL", "AMD", "AMZN", "ARM", "AVGO", "COIN", "CRCL", "GOOG",
    "GOOGL", "HOOD", "INTC", "KORU", "META", "MSFT", "MSTR", "MU",
    "NFLX", "NVDA", "ORCL", "PLTR", "QQQ", "SKHY", "SNDK", "SNXX",
    "SOXL", "SOXS", "SPCX", "SPY", "TQQQ", "TSLA", "UBER",
}


def non_crypto_reason(base):
    """Why this base asset is excluded from the TRADING universe, or
    None if it is a genuine crypto asset that may be traded."""
    if base in NON_CRYPTO_BASES:
        return "gold token, not crypto"
    if base.endswith("B") and base[:-1] in BSTOCK_TICKERS:
        return "tokenized stock (bStock), not crypto"
    return None


def is_non_crypto_base(base):
    return non_crypto_reason(base) is not None


def build_trading_universe(top_n=None, result=None):
    """The analysis set for one screener-mode cycle: the top `top_n`
    ranked coins with non-crypto pairs removed, plus the majors
    (BTC/ETH/SOL) always included even if they ranked below the cut.
    Analysis cap is therefore top_n + len(MAJORS) symbols.

    `result` may carry a screener.run_screener() result to reuse;
    otherwise the screener is run live. A screener fetch failure
    RAISES - callers must fail closed (open nothing), never fall back
    to a stale universe.
    """
    top_n = top_n or config.SCREENER_TOP_N
    if result is None:
        result = run_screener()
    kept, excluded = [], []
    for r in result["eligible"]:
        if is_non_crypto_base(r["base"]):
            excluded.append(r["symbol"])
        else:
            kept.append(r["symbol"])
    symbols = kept[:top_n]
    for m in MAJORS:
        if m not in symbols:
            symbols.append(m)
    return {"symbols": symbols, "excluded_non_crypto": excluded,
            "eligible_count": len(result["eligible"]),
            "fetched_at_ms": result["fetched_at_ms"],
            "host": result["host"]}


def load_universe():
    """Symbol dictionary {base: pair} for events.py. Uses the screener's
    last output if present; otherwise fetches the universe live."""
    path = config.SCREENER_UNIVERSE_PATH
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)["universe"]
    result = run_screener(write=True)
    return {r["base"]: r["symbol"] for r in result["eligible"]}


if __name__ == "__main__":
    res = run_screener()
    print(f"Screener: {res['stages']} via {res['host']}")
    print(f"Top {len(res['top'])}:")
    for r in res["top"]:
        print(f"  {r['rank']:>2}. {r['symbol']:<14} "
              f"price={r['last_price']:g} 24h={r['change_pct']:+.2f}% "
              f"qvol={r['quote_volume']:,.0f} score={r['score']:.3f}")
    print(f"Wrote {config.SCREENER_REPORT_PATH} and "
          f"{config.SCREENER_UNIVERSE_PATH}")
