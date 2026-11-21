"""Performance metrics for one bot, computed read-only from its journal.

Metrics: total PnL, win rate, profit factor, max drawdown, Sharpe ratio.

Equity curve: start equity, then every realised cash event in time
order - each partial profit booking at its timestamp, and each trade
close contributing (final pnl - partials already counted). Unrealised
PnL of open positions is never counted.

Sharpe: daily realised returns (each UTC day's realised PnL divided by
the equity at the start of that day), annualised with sqrt(365) since
crypto trades every day. Needs at least 2 days with realised activity
and non-zero variance, otherwise reported as None ("not enough data") -
never fabricated.
"""

import json
import math
import sqlite3


def _load(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY id")]
        points = [dict(r) for r in conn.execute(
            "SELECT * FROM equity_points ORDER BY ts_ms")]
    finally:
        conn.close()
    return trades, points


def equity_curve(db_path, start_equity, start_ts_ms=0):
    trades, _ = _load(db_path)
    events = []  # (ts_ms, delta)
    for t in trades:
        try:
            partials = json.loads(t.get("partials") or "[]")
        except ValueError:
            partials = []
        partial_sum = 0.0
        for p in partials:
            events.append((p["ts_ms"], p["pnl"]))
            partial_sum += p["pnl"]
        if t["status"] == "closed" and t.get("pnl") is not None:
            events.append((t["exit_ts_ms"], t["pnl"] - partial_sum))
    events.sort(key=lambda e: e[0])
    curve = [{"ts_ms": start_ts_ms, "equity": round(start_equity, 2)}]
    equity = start_equity
    for ts, delta in events:
        equity += delta
        curve.append({"ts_ms": ts, "equity": round(equity, 2)})
    return curve


def compute(db_path, start_equity, start_ts_ms=0):
    trades, _ = _load(db_path)
    closed = [t for t in trades if t["status"] == "closed"]
    open_trades = [t for t in trades if t["status"] == "open"]

    total_pnl = sum(t["pnl"] for t in closed) + \
        sum(t.get("partial_pnl") or 0.0 for t in open_trades)
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    win_rate = (len(wins) / len(closed)) if closed else None
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    total_fees = sum(t.get("fees_paid") or 0.0 for t in trades)

    curve = equity_curve(db_path, start_equity, start_ts_ms)
    peak, max_dd = start_equity, 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        if peak > 0:
            max_dd = max(max_dd, (peak - pt["equity"]) / peak)

    # Daily realised returns for Sharpe.
    daily = {}
    equity = start_equity
    day_start_equity = {}
    events = []
    for t in trades:
        try:
            partials = json.loads(t.get("partials") or "[]")
        except ValueError:
            partials = []
        partial_sum = 0.0
        for p in partials:
            events.append((p["ts_ms"], p["pnl"]))
            partial_sum += p["pnl"]
        if t["status"] == "closed" and t.get("pnl") is not None:
            events.append((t["exit_ts_ms"], t["pnl"] - partial_sum))
    events.sort(key=lambda e: e[0])
    for ts, delta in events:
        day = ts // 86_400_000
        if day not in day_start_equity:
            day_start_equity[day] = equity
        daily[day] = daily.get(day, 0.0) + delta
        equity += delta
    returns = [daily[d] / day_start_equity[d]
               for d in sorted(daily) if day_start_equity[d] > 0]
    sharpe = None
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        sd = math.sqrt(var)
        if sd > 0:
            sharpe = round(mean / sd * math.sqrt(365), 3)

    return {
        "start_equity": start_equity,
        "equity": round(start_equity + total_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "trades_total": len(trades),
        "trades_closed": len(closed),
        "trades_open": len(open_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "profit_factor": round(profit_factor, 3)
        if profit_factor is not None else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": sharpe,
        "equity_curve": curve,
    }
