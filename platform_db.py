"""AlphaSwarm platform database: users, sessions, bots, payments, audit.

One SQLite file for the whole platform; each bot additionally owns a
separate engine journal DB under data/bots/<bot_id>/journal.db.

Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes. There is no
table for exchange API keys anywhere in this platform - by design.
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    salt TEXT NOT NULL,
    pass_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',       -- 'admin' | 'user'
    plan TEXT NOT NULL DEFAULT 'free',       -- 'free' | 'pro' | 'premium'
    plan_status TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'v1' | 'v2' | 'event' | 'breakout' | 'router' | 'range'
    status TEXT NOT NULL DEFAULT 'stopped',  -- running|paused|stopped
    published INTEGER NOT NULL DEFAULT 0,
    start_equity REAL NOT NULL DEFAULT 10000,
    symbols TEXT NOT NULL DEFAULT '["BTCUSDT","ETHUSDT","SOLUSDT"]',
    risk_pct REAL NOT NULL DEFAULT 0.01,
    daily_loss_pct REAL NOT NULL DEFAULT 0.03,
    universe_mode TEXT NOT NULL DEFAULT 'fixed',  -- 'fixed' | 'screener'
    universe_size INTEGER NOT NULL DEFAULT 20,    -- screener top N (5-30)
    cycle_minutes INTEGER NOT NULL DEFAULT 0,  -- 0 = manual only
    created_by INTEGER,
    created_at INTEGER NOT NULL,
    last_cycle_at INTEGER,
    last_cycle_summary TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    amount REAL NOT NULL,
    card_last4 TEXT,
    status TEXT NOT NULL,            -- 'test_paid' (TEST MODE checkout)
    ts_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS follows (
    user_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,
    PRIMARY KEY (user_id, bot_id)
);
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    added_ms INTEGER NOT NULL,
    PRIMARY KEY (user_id, symbol)
);
CREATE TABLE IF NOT EXISTS signal_usage (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,               -- UTC day, 'YYYY-MM-DD'
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

PLANS = {
    "free": {"name": "Free", "price": 0,
             "features": ["Paper-trading dashboard",
                          "Published bot performance (summary)",
                          "2 live signal searches a day",
                          "Your own paper account view"]},
    "pro": {"name": "Pro", "price": 19,
            "features": ["Everything in Free",
                         "Unlimited live signal searches",
                         "Live signals with the agents' reasons",
                         "News feed with confirmed/unconfirmed badges",
                         "Confirmed-event alerts"]},
    "premium": {"name": "Premium", "price": 49,
                "features": ["Everything in Pro",
                             "Bot comparison and full trade history",
                             "Auto-trading: NOT YET AVAILABLE - "
                             "this platform is paper-trading only"]},
}

# Bot columns added after the original schema; see PlatformDB.__init__.
_BOT_EXTRA_COLUMNS = {
    "universe_mode": "TEXT NOT NULL DEFAULT 'fixed'",
    "universe_size": "INTEGER NOT NULL DEFAULT 20",
}

_PBKDF2_ROUNDS = 200_000
SESSION_TTL_MS = 7 * 24 * 3600 * 1000


def now_ms():
    return int(time.time() * 1000)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt),
        _PBKDF2_ROUNDS).hex()
    return salt, digest


def check_password(password, salt, expected):
    _, digest = hash_password(password, salt)
    return hmac.compare_digest(digest, expected)


class PlatformDB:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Columns added after the original schema (for databases created
        # by an older copy); applied idempotently on open.
        existing = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(bots)")}
        for col, decl in _BOT_EXTRA_COLUMNS.items():
            if col not in existing:
                self.conn.execute(
                    f"ALTER TABLE bots ADD COLUMN {col} {decl}")
        self.conn.commit()

    # ---- users & sessions ---------------------------------------------
    def create_user(self, email, name, password):
        email = email.strip().lower()
        count = self.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        role = "admin" if count == 0 else "user"  # first user = the owner
        salt, digest = hash_password(password)
        cur = self.conn.execute(
            "INSERT INTO users (email, name, salt, pass_hash, role,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (email, name, salt, digest, role, now_ms()))
        self.conn.commit()
        return self.get_user(cur.lastrowid)

    def get_user(self, user_id):
        row = self.conn.execute("SELECT * FROM users WHERE id=?",
                                (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email):
        row = self.conn.execute("SELECT * FROM users WHERE email=?",
                                (email.strip().lower(),)).fetchone()
        return dict(row) if row else None

    def list_users(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT id, email, name, role, plan, plan_status, created_at"
            " FROM users ORDER BY id")]

    def create_session(self, user_id):
        token = secrets.token_urlsafe(32)
        ts = now_ms()
        self.conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at)"
            " VALUES (?,?,?,?)", (token, user_id, ts, ts + SESSION_TTL_MS))
        self.conn.commit()
        return token

    def user_for_token(self, token):
        if not token:
            return None
        row = self.conn.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token=?",
            (token,)).fetchone()
        if not row or row["expires_at"] < now_ms():
            return None
        return self.get_user(row["user_id"])

    def drop_session(self, token):
        self.conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        self.conn.commit()

    def set_plan(self, user_id, plan):
        self.conn.execute(
            "UPDATE users SET plan=?, plan_status='active' WHERE id=?",
            (plan, user_id))
        self.conn.commit()

    # ---- bots -----------------------------------------------------------
    def create_bot(self, name, kind, start_equity, symbols, risk_pct,
                   daily_loss_pct, cycle_minutes, created_by,
                   universe_mode="fixed", universe_size=20):
        import json
        cur = self.conn.execute(
            "INSERT INTO bots (name, kind, start_equity, symbols, risk_pct,"
            " daily_loss_pct, cycle_minutes, created_by, created_at,"
            " universe_mode, universe_size)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, kind, start_equity, json.dumps(symbols), risk_pct,
             daily_loss_pct, cycle_minutes, created_by, now_ms(),
             universe_mode, universe_size))
        self.conn.commit()
        return self.get_bot(cur.lastrowid)

    def get_bot(self, bot_id):
        row = self.conn.execute("SELECT * FROM bots WHERE id=?",
                                (bot_id,)).fetchone()
        return dict(row) if row else None

    def list_bots(self, published_only=False):
        q = "SELECT * FROM bots"
        if published_only:
            q += " WHERE published=1"
        return [dict(r) for r in self.conn.execute(q + " ORDER BY id")]

    def update_bot(self, bot_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE bots SET {cols} WHERE id=?",
                          (*fields.values(), bot_id))
        self.conn.commit()

    # ---- payments / follows / audit / settings --------------------------
    def record_payment(self, user_id, plan, amount, card_last4):
        cur = self.conn.execute(
            "INSERT INTO payments (user_id, plan, amount, card_last4,"
            " status, ts_ms) VALUES (?,?,?,?,?,?)",
            (user_id, plan, amount, card_last4, "test_paid", now_ms()))
        self.conn.commit()
        return cur.lastrowid

    def follow(self, user_id, bot_id):
        self.conn.execute(
            "INSERT OR IGNORE INTO follows (user_id, bot_id, ts_ms)"
            " VALUES (?,?,?)", (user_id, bot_id, now_ms()))
        self.conn.commit()

    # ---- watchlist ------------------------------------------------------
    def watchlist_add(self, user_id, symbol):
        self.conn.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, symbol, added_ms)"
            " VALUES (?,?,?)", (user_id, symbol, now_ms()))
        self.conn.commit()

    def watchlist_remove(self, user_id, symbol):
        self.conn.execute(
            "DELETE FROM watchlist WHERE user_id=? AND symbol=?",
            (user_id, symbol))
        self.conn.commit()

    def watchlist_list(self, user_id):
        return [r["symbol"] for r in self.conn.execute(
            "SELECT symbol FROM watchlist WHERE user_id=?"
            " ORDER BY added_ms, symbol", (user_id,))]

    # ---- signal search quota (Free plan: N searches per UTC day) --------
    def signal_usage_count(self, user_id, day):
        row = self.conn.execute(
            "SELECT count FROM signal_usage WHERE user_id=? AND day=?",
            (user_id, day)).fetchone()
        return row["count"] if row else 0

    def signal_usage_bump(self, user_id, day):
        self.conn.execute(
            "INSERT INTO signal_usage (user_id, day, count) VALUES (?,?,1)"
            " ON CONFLICT(user_id, day) DO UPDATE SET count=count+1",
            (user_id, day))
        self.conn.commit()
        return self.signal_usage_count(user_id, day)

    def audit(self, user_id, action, detail=""):
        self.conn.execute(
            "INSERT INTO audit_log (ts_ms, user_id, action, detail)"
            " VALUES (?,?,?,?)", (now_ms(), user_id, action, detail))
        self.conn.commit()

    def list_audit(self, limit=100):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]

    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?",
                                (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
            (key, value))
        self.conn.commit()


def public_user(user):
    """User dict safe to send to the browser (no salt/hash)."""
    if not user:
        return None
    return {k: user[k] for k in
            ("id", "email", "name", "role", "plan", "plan_status",
             "created_at")}
