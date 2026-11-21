"""SQLite trade journal: cycles, votes, proposals, trades, equity curve.

Platform copy of the AlphaPaper journal, extended for the AlphaSwarm
platform: every trade stores a plain-language entry reason and exit
detail, partial profit bookings, accumulated paper fees, and the state
the trailing stop needs (best price seen, TP1 flag). This journal is the
bot's only 'account'. There is no external account.
"""

import json
import os
import sqlite3
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER, symbol TEXT, interval TEXT, source TEXT,
    price REAL, action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER, ts_ms INTEGER, symbol TEXT, agent TEXT,
    direction TEXT, confidence REAL, evidence TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER, ts_ms INTEGER, symbol TEXT, direction TEXT,
    entry REAL, stop_loss REAL, take_profit REAL, avg_confidence REAL,
    status TEXT, reasons TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER, symbol TEXT, direction TEXT,
    entry_ts_ms INTEGER, entry_price REAL, qty REAL,
    stop_loss REAL, take_profit REAL, risk_amount REAL,
    status TEXT DEFAULT 'open',
    exit_ts_ms INTEGER, exit_price REAL, exit_reason TEXT, pnl REAL,
    qty_orig REAL, risk_dist REAL, tp1_price REAL,
    tp1_done INTEGER DEFAULT 0, best_price REAL,
    partial_pnl REAL DEFAULT 0, partials TEXT DEFAULT '[]',
    fees_paid REAL DEFAULT 0, entry_reason TEXT, exit_detail TEXT
);
CREATE TABLE IF NOT EXISTS equity_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER, equity REAL, note TEXT
);
"""

# Columns added after the original schema (for journals created by an
# older copy); applied idempotently on open.
_EXTRA_TRADE_COLUMNS = {
    "qty_orig": "REAL", "risk_dist": "REAL", "tp1_price": "REAL",
    "tp1_done": "INTEGER DEFAULT 0", "best_price": "REAL",
    "partial_pnl": "REAL DEFAULT 0", "partials": "TEXT DEFAULT '[]'",
    "fees_paid": "REAL DEFAULT 0", "entry_reason": "TEXT",
    "exit_detail": "TEXT",
}


class Journal:
    def __init__(self, path=None):
        self.path = path or config.DB_PATH
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        existing = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(trades)")}
        for col, decl in _EXTRA_TRADE_COLUMNS.items():
            if col not in existing:
                self.conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {col} {decl}")
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- writing -------------------------------------------------------
    def log_cycle(self, symbol, interval, source, price, action, detail=""):
        cur = self.conn.execute(
            "INSERT INTO cycles (ts_ms, symbol, interval, source, price,"
            " action, detail) VALUES (?,?,?,?,?,?,?)",
            (int(time.time() * 1000), symbol, interval, source, price,
             action, detail))
        self.conn.commit()
        return cur.lastrowid

    def log_votes(self, cycle_id, symbol, votes):
        ts = int(time.time() * 1000)
        self.conn.executemany(
            "INSERT INTO votes (cycle_id, ts_ms, symbol, agent, direction,"
            " confidence, evidence) VALUES (?,?,?,?,?,?,?)",
            [(cycle_id, ts, symbol, v["agent"], v["direction"],
              v["confidence"], json.dumps(v["evidence"])) for v in votes])
        self.conn.commit()

    def log_proposal(self, cycle_id, proposal, status, reasons):
        cur = self.conn.execute(
            "INSERT INTO proposals (cycle_id, ts_ms, symbol, direction,"
            " entry, stop_loss, take_profit, avg_confidence, status, reasons)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, int(time.time() * 1000), proposal["symbol"],
             proposal["direction"], proposal["entry"], proposal["stop_loss"],
             proposal["take_profit"], proposal["avg_confidence"], status,
             json.dumps(reasons)))
        self.conn.commit()
        return cur.lastrowid

    def open_trade(self, proposal_id, proposal, qty, risk_amount, entry_ts_ms,
                   entry_reason="", tp1_price=None, entry_fee=0.0):
        entry = proposal["entry"]
        risk_dist = abs(entry - proposal["stop_loss"])
        cur = self.conn.execute(
            "INSERT INTO trades (proposal_id, symbol, direction, entry_ts_ms,"
            " entry_price, qty, stop_loss, take_profit, risk_amount,"
            " qty_orig, risk_dist, tp1_price, best_price, fees_paid,"
            " entry_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, proposal["symbol"], proposal["direction"],
             entry_ts_ms, entry, qty, proposal["stop_loss"],
             proposal["take_profit"], risk_amount, qty, risk_dist,
             tp1_price, entry, entry_fee, entry_reason))
        self.conn.commit()
        return cur.lastrowid

    def update_trade(self, trade_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE trades SET {cols} WHERE id=?",
                          (*fields.values(), trade_id))
        self.conn.commit()

    def record_partial(self, trade_id, qty_closed, exit_price, ts_ms,
                       net_pnl, fee, note):
        """Book a partial exit: shrink the remaining qty, accumulate the
        realised (net-of-fee) partial PnL and append an audit record."""
        row = self.conn.execute("SELECT * FROM trades WHERE id=?",
                                (trade_id,)).fetchone()
        partials = json.loads(row["partials"] or "[]")
        partials.append({"ts_ms": ts_ms, "qty": qty_closed,
                         "price": exit_price, "pnl": round(net_pnl, 2),
                         "fee": round(fee, 2), "note": note})
        self.conn.execute(
            "UPDATE trades SET qty=?, partial_pnl=?, partials=?, fees_paid=?"
            " WHERE id=?",
            (row["qty"] - qty_closed,
             round((row["partial_pnl"] or 0.0) + net_pnl, 2),
             json.dumps(partials),
             round((row["fees_paid"] or 0.0) + fee, 2), trade_id))
        self.conn.commit()

    def close_trade(self, trade_id, exit_ts_ms, exit_price, exit_reason, pnl,
                    exit_detail=""):
        self.conn.execute(
            "UPDATE trades SET status='closed', exit_ts_ms=?, exit_price=?,"
            " exit_reason=?, pnl=?, exit_detail=? WHERE id=?",
            (exit_ts_ms, exit_price, exit_reason, pnl, exit_detail, trade_id))
        self.conn.commit()

    def trade_exists_for_signal(self, symbol, entry_ts_ms):
        """True if a trade (open or closed) was already opened from the
        signal bar identified by entry_ts_ms for this symbol. The
        breakout engine uses this so re-running a cycle inside the
        same hour cannot open a duplicate trade off the same
        breakout bar."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE symbol=?"
            " AND entry_ts_ms=?", (symbol, entry_ts_ms)).fetchone()
        return row["n"] > 0

    def get_trade(self, trade_id):
        row = self.conn.execute("SELECT * FROM trades WHERE id=?",
                                (trade_id,)).fetchone()
        return dict(row) if row else None

    def log_equity(self, equity, note=""):
        self.conn.execute(
            "INSERT INTO equity_points (ts_ms, equity, note) VALUES (?,?,?)",
            (int(time.time() * 1000), equity, note))
        self.conn.commit()

    # ---- reading -------------------------------------------------------
    def open_trades(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY id")]

    def closed_trades(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts_ms")]

    def recent_trades(self, limit=20):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))]

    def all_trades(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM trades ORDER BY id")]

    def votes_for_proposal(self, proposal_id):
        """Agent votes recorded on the cycle that produced a proposal."""
        row = self.conn.execute(
            "SELECT cycle_id FROM proposals WHERE id=?",
            (proposal_id,)).fetchone()
        if not row:
            return []
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM votes WHERE cycle_id=? ORDER BY id",
            (row["cycle_id"],))]

    def latest_cycle_votes(self):
        row = self.conn.execute(
            "SELECT MAX(id) AS mid FROM cycles").fetchone()
        if not row or row["mid"] is None:
            return [], None
        cycle = dict(self.conn.execute(
            "SELECT * FROM cycles WHERE id=?", (row["mid"],)).fetchone())
        votes = [dict(r) for r in self.conn.execute(
            "SELECT * FROM votes WHERE cycle_id=?", (row["mid"],))]
        return votes, cycle

    def equity(self):
        """Start equity + realised PnL. A closed trade's pnl already
        includes its partial bookings; open trades contribute only the
        partial profit already booked (never unrealised PnL)."""
        closed = self.conn.execute(
            "SELECT COALESCE(SUM(pnl),0) AS s FROM trades"
            " WHERE status='closed'").fetchone()["s"]
        open_partials = self.conn.execute(
            "SELECT COALESCE(SUM(partial_pnl),0) AS s FROM trades"
            " WHERE status='open'").fetchone()["s"]
        return config.START_EQUITY + closed + open_partials

    def daily_state(self, now_ms=None):
        """Equity, today's realised PnL and day-start equity (UTC day)."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        day_start_ms = now_ms - (now_ms % 86_400_000)
        daily_pnl = self.conn.execute(
            "SELECT COALESCE(SUM(pnl),0) AS s FROM trades"
            " WHERE status='closed' AND exit_ts_ms>=?",
            (day_start_ms,)).fetchone()["s"]
        equity = self.equity()
        return {"equity": equity,
                "open_positions": len(self.open_trades()),
                "daily_pnl": daily_pnl,
                "day_start_equity": equity - daily_pnl}
