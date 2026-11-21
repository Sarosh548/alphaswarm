#!/usr/bin/env python3
"""AlphaSwarm platform server (Python standard library only).

    python3 app.py            # http://127.0.0.1:8000

PAPER TRADING ONLY. This platform contains no real-money execution, no
exchange order placement and no exchange API-key handling of any kind.
Billing is a TEST-MODE simulated checkout (no real payment provider is
connected); a test card always succeeds and is labelled as such.

The FIRST user to sign up becomes the admin (the owner). Everyone else
is a subscriber.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

import backup as backup_service
import botrunner
import metrics
from platform_db import PLANS, PlatformDB, now_ms, public_user

# engine/signals.py - read-only live signals for the signal-first home
# page. botrunner has already put ENGINE_DIR on sys.path.
import signals as signals_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = botrunner.DATA_DIR
PORT = int(os.environ.get("ALPHASWARM_PORT", "8000"))

db = PlatformDB(os.path.join(DATA_DIR, "platform.db"))

RISK_DISCLAIMER = ("Paper trading only: all balances and results on this "
                   "platform are simulated. Past paper performance is not "
                   "a promise of real returns, and no result here is "
                   "financial advice.")

# Free plan packaging (Tradebise-style): a Free user gets this many
# single-market signal searches per UTC day. Admin and Pro/Premium
# are unlimited. The /api/signals overview grid and the watchlist do
# NOT consume quota - only /api/signal searches count.
FREE_SIGNAL_DAILY_LIMIT = 2

_price_cache = {}


def latest_price(symbol):
    """Last closed hourly candle close, cached 60s. (symbol, ts_ms)"""
    now = time.time()
    hit = _price_cache.get(symbol)
    if hit and now - hit[1] < 60:
        return hit[0]
    try:
        candles, _ = botrunner.market.get_candles(symbol, "1h", 5)
        price = candles[-1]["close"] if candles else None
    except Exception:
        price = None
    _price_cache[symbol] = (price, now)
    return price


def impact_label(ev):
    """Rule-based market-impact label for a news event (platform v1:
    keyword/category rules from the v3 event agent - honestly labelled
    as rule-based in the UI, not an AI prediction)."""
    label = {"positive": "Bullish", "negative": "Bearish"}.get(
        ev.get("sentiment"), "Neutral")
    cat = ev.get("category")
    if cat in ("HACK_OR_EXPLOIT", "DELISTING") or ev.get("strong_positive"):
        impact = "High"
    elif cat in ("ETF_OR_INSTITUTIONAL", "EXCHANGE_LISTING", "REGULATION",
                 "MACRO"):
        impact = "Medium"
    else:
        impact = "Low"
    return label, impact


def bot_visible(user, bot):
    if user["role"] == "admin":
        return True
    return bool(bot["published"])


def can_see_reasons(user, bot):
    """Signals with the agents' reasons: admin always; subscribers on
    Pro/Premium plans (Free sees performance summaries only)."""
    if user["role"] == "admin":
        return True
    return user["plan"] in ("pro", "premium")


def bot_summary(bot):
    path = botrunner.journal_path(bot["id"])
    if os.path.exists(path):
        m = metrics.compute(path, float(bot["start_equity"]),
                            bot["created_at"])
    else:
        m = {"equity": bot["start_equity"], "total_pnl": 0.0,
             "win_rate": None, "profit_factor": None,
             "max_drawdown_pct": 0.0, "sharpe": None, "trades_open": 0,
             "trades_closed": 0, "total_fees": 0.0}
    return {
        "id": bot["id"], "name": bot["name"], "kind": bot["kind"],
        "status": bot["status"], "published": bool(bot["published"]),
        "start_equity": bot["start_equity"],
        "symbols": json.loads(bot["symbols"]),
        "risk_pct": bot["risk_pct"],
        "daily_loss_pct": bot["daily_loss_pct"],
        "universe_mode": bot.get("universe_mode") or "fixed",
        "universe_size": bot.get("universe_size") or 20,
        "cycle_minutes": bot["cycle_minutes"],
        "last_cycle_at": bot["last_cycle_at"],
        "last_cycle_summary": bot["last_cycle_summary"] or "",
        "metrics": {k: m[k] for k in
                    ("equity", "total_pnl", "win_rate", "profit_factor",
                     "max_drawdown_pct", "sharpe", "trades_open",
                     "trades_closed", "total_fees")},
    }


def trade_view(trade, with_reason=True):
    try:
        partials = json.loads(trade.get("partials") or "[]")
    except ValueError:
        partials = []
    view = {
        "id": trade["id"], "symbol": trade["symbol"],
        "direction": trade["direction"], "status": trade["status"],
        "entry_price": trade["entry_price"],
        "entry_ts_ms": trade["entry_ts_ms"],
        "qty": trade["qty"], "qty_orig": trade.get("qty_orig"),
        "stop_loss": trade["stop_loss"],
        "take_profit": trade["take_profit"],
        "tp1_price": trade.get("tp1_price"),
        "tp1_done": bool(trade.get("tp1_done")),
        "risk_amount": trade["risk_amount"],
        "fees_paid": trade.get("fees_paid") or 0.0,
        "partial_pnl": trade.get("partial_pnl") or 0.0,
        "partials": partials,
        "exit_price": trade.get("exit_price"),
        "exit_ts_ms": trade.get("exit_ts_ms"),
        "exit_reason": trade.get("exit_reason"),
        "pnl": trade.get("pnl"),
    }
    if with_reason:
        view["entry_reason"] = trade.get("entry_reason") or ""
        view["exit_detail"] = trade.get("exit_detail") or ""
    return view


def read_bot_trades(bot):
    path = botrunner.journal_path(bot["id"])
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY id DESC")]
    finally:
        conn.close()


def latest_votes_by_symbol(bot):
    path = botrunner.journal_path(bot["id"])
    if not os.path.exists(path):
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out = {}
        symbols = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM cycles")]
        for sym in symbols:
            cyc = conn.execute(
                "SELECT * FROM cycles WHERE symbol=? ORDER BY id DESC"
                " LIMIT 1", (sym,)).fetchone()
            if not cyc:
                continue
            votes = [dict(r) for r in conn.execute(
                "SELECT agent, direction, confidence, evidence FROM votes"
                " WHERE cycle_id=? ORDER BY id", (cyc["id"],))]
            for v in votes:
                try:
                    v["evidence"] = json.loads(v["evidence"] or "[]")
                except ValueError:
                    v["evidence"] = []
            out[sym] = {"cycle": dict(cyc), "votes": votes}
        return out
    finally:
        conn.close()


def load_news(limit=100):
    import sys
    if botrunner.ENGINE_DIR not in sys.path:
        sys.path.insert(0, botrunner.ENGINE_DIR)
    import events as events_mod
    path = os.path.join(DATA_DIR, "events.jsonl")
    pool = events_mod.load_events(path=path)
    pool.sort(key=lambda e: e.get("published_ts_ms", 0), reverse=True)
    out = []
    for ev in pool[:limit]:
        label, impact = impact_label(ev)
        out.append({
            "event_id": ev["event_id"], "headline": ev["headline"],
            "url": ev.get("url", ""), "source": ev.get("source", ""),
            "category": ev.get("category", "OTHER"),
            "symbols": ev.get("symbols", []),
            "published_ts_ms": ev.get("published_ts_ms", 0),
            "confirmation": ev.get("confirmation", "UNCONFIRMED"),
            "impact_label": label, "impact": impact,
            "sentiment_method": "rule-based (keyword/category v1)",
        })
    alerts = []
    apath = os.path.join(DATA_DIR, "alerts.jsonl")
    if os.path.exists(apath):
        with open(apath, encoding="utf-8") as f:
            for line in f:
                try:
                    alerts.append(json.loads(line))
                except ValueError:
                    continue
    alerts = alerts[-20:][::-1]
    fg = None
    try:
        fg = events_mod.get_fear_greed()
    except Exception:
        fg = None
    return out, alerts, fg


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_access_logger = None


def access_logger():
    """HTTP access log, written to <data_dir>/logs/access.log with
    hard rotation (1 MB x 3 files), so server logs can never grow
    without bound."""
    global _access_logger
    if _access_logger is None:
        logger = logging.getLogger("alphaswarm.access")
        try:
            log_dir = os.path.join(DATA_DIR, "logs")
            os.makedirs(log_dir, exist_ok=True)
            handler = RotatingFileHandler(
                os.path.join(log_dir, "access.log"),
                maxBytes=1_000_000, backupCount=3)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        except Exception:
            logger.addHandler(logging.NullHandler())
        _access_logger = logger
    return _access_logger


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, format, *args):
        try:
            access_logger().info("%s - %s",
                                 self.address_string(), format % args)
        except Exception:
            pass

    def _send_json(self, obj, status=200, cookie=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _user(self):
        cookie = self.headers.get("Cookie") or ""
        token = None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("session="):
                token = part[len("session="):]
        return db.user_for_token(token), token

    def _require_user(self):
        user, token = self._user()
        if not user:
            self._send_json({"error": "Please log in first."}, 401)
            return None, None
        return user, token

    def _require_admin(self):
        user, token = self._require_user()
        if user is None:
            return None, None
        if user["role"] != "admin":
            self._send_json({"error": "Admin only."}, 403)
            return None, None
        return user, token

    # -- routing ------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                return self._send_html(
                    os.path.join(BASE_DIR, "static", "index.html"))
            if path == "/api/signal":
                return self.api_signal()
            if path == "/api/signals":
                return self.api_signals()
            routes = {
                "/api/me": self.api_me,
                "/api/home": self.api_home,
                "/api/bots": self.api_bots,
                "/api/public/bots": self.api_public_bots,
                "/api/trades/open": self.api_open_trades,
                "/api/news": self.api_news,
                "/api/compare": self.api_compare,
                "/api/plans": self.api_plans,
                "/api/watchlist": self.api_watchlist,
                "/api/record": self.api_record,
                "/api/admin/users": self.api_admin_users,
                "/api/admin/logs": self.api_admin_logs,
                "/api/admin/backup/download":
                    self.api_admin_backup_download,
            }
            if path in routes:
                return routes[path]()
            if path.startswith("/api/bots/"):
                return self.api_bot_detail(int(path.split("/")[3]))
            self._send_json({"error": "Not found."}, 404)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            routes = {
                "/api/signup": self.api_signup,
                "/api/login": self.api_login,
                "/api/logout": self.api_logout,
                "/api/bots": self.api_create_bot,
                "/api/checkout": self.api_checkout,
                "/api/watchlist": self.api_watchlist_add,
                "/api/watchlist/remove": self.api_watchlist_remove,
                "/api/admin/backup": self.api_admin_backup_create,
                "/api/kill/global": self.api_kill_global,
                "/api/kill/global/clear": self.api_kill_global_clear,
            }
            if path in routes:
                return routes[path]()
            parts = path.split("/")
            # /api/bots/<id>/<action>
            if len(parts) == 5 and parts[1:3] == ["api", "bots"]:
                return self.api_bot_action(int(parts[3]), parts[4])
            # /api/trades/<bot_id>/<trade_id>/close
            if len(parts) == 6 and parts[1:3] == ["api", "trades"] \
                    and parts[5] == "close":
                return self.api_close_trade(int(parts[3]), int(parts[4]))
            self._send_json({"error": "Not found."}, 404)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- auth ---------------------------------------------------------------
    def api_signup(self):
        body = self._body()
        email = (body.get("email") or "").strip()
        name = (body.get("name") or "").strip()
        password = body.get("password") or ""
        if "@" not in email or not name or len(password) < 6:
            return self._send_json(
                {"error": "Please give a valid email, your name, and a "
                          "password of at least 6 characters."}, 400)
        if db.get_user_by_email(email):
            return self._send_json(
                {"error": "That email is already registered. Log in "
                          "instead."}, 409)
        user = db.create_user(email, name, password)
        db.audit(user["id"], "signup",
                 f"role={user['role']}")
        token = db.create_session(user["id"])
        self._send_json({"user": public_user(user)},
                        cookie=f"session={token}; HttpOnly; Path=/")

    def api_login(self):
        body = self._body()
        user = db.get_user_by_email(body.get("email") or "")
        if not user:
            return self._send_json({"error": "No account with that "
                                             "email."}, 401)
        from platform_db import check_password
        if not check_password(body.get("password") or "", user["salt"],
                              user["pass_hash"]):
            return self._send_json({"error": "Wrong password."}, 401)
        db.audit(user["id"], "login", "")
        token = db.create_session(user["id"])
        self._send_json({"user": public_user(user)},
                        cookie=f"session={token}; HttpOnly; Path=/")

    def api_logout(self):
        _, token = self._user()
        if token:
            db.drop_session(token)
        self._send_json({"ok": True},
                        cookie="session=; HttpOnly; Path=/; Max-Age=0")

    def api_me(self):
        user, _ = self._require_user()
        if user:
            self._send_json({"user": public_user(user),
                             "global_halt": db.get_setting("global_halt")
                             == "1"})

    # -- read APIs ------------------------------------------------------------
    def api_home(self):
        user, _ = self._require_user()
        if not user:
            return
        bots = [b for b in db.list_bots() if bot_visible(user, b)]
        summaries = [bot_summary(b) for b in bots]
        total_pnl = sum(s["metrics"]["total_pnl"] for s in summaries)
        closed = sum(s["metrics"]["trades_closed"] for s in summaries)
        wins = 0
        for b in bots:
            for t in read_bot_trades(b):
                if t["status"] == "closed" and (t.get("pnl") or 0) > 0:
                    wins += 1
        _, _, fg = load_news(limit=1)
        self._send_json({
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(wins / closed, 4) if closed else None,
            "open_trades": sum(s["metrics"]["trades_open"]
                               for s in summaries),
            "closed_trades": closed,
            "bots": summaries,
            "fear_greed": fg,
            "global_halt": db.get_setting("global_halt") == "1",
            "disclaimer": RISK_DISCLAIMER,
        })

    def api_bots(self):
        user, _ = self._require_user()
        if not user:
            return
        bots = [bot_summary(b) for b in db.list_bots()
                if bot_visible(user, b)]
        self._send_json({"bots": bots})

    def api_public_bots(self):
        user, _ = self._require_user()
        if not user:
            return
        bots = [bot_summary(b) for b in db.list_bots(published_only=True)]
        self._send_json({"bots": bots, "disclaimer": RISK_DISCLAIMER})

    def api_bot_detail(self, bot_id):
        user, _ = self._require_user()
        if not user:
            return
        bot = db.get_bot(bot_id)
        if not bot or not bot_visible(user, bot):
            return self._send_json({"error": "Bot not found."}, 404)
        summary = bot_summary(bot)
        path = botrunner.journal_path(bot_id)
        curve = []
        if os.path.exists(path):
            curve = metrics.equity_curve(path, float(bot["start_equity"]),
                                         bot["created_at"])
        reasons = can_see_reasons(user, bot)
        trades = [trade_view(t, with_reason=reasons)
                  for t in read_bot_trades(bot)][:100]
        self._send_json({
            "bot": summary, "equity_curve": curve, "trades": trades,
            "latest_votes": latest_votes_by_symbol(bot),
            "reasons_visible": reasons,
            "disclaimer": RISK_DISCLAIMER,
        })

    def api_open_trades(self):
        user, _ = self._require_user()
        if not user:
            return
        out = []
        for bot in db.list_bots():
            if not bot_visible(user, bot):
                continue
            reasons = can_see_reasons(user, bot)
            for t in read_bot_trades(bot):
                if t["status"] != "open":
                    continue
                view = trade_view(t, with_reason=reasons)
                price = latest_price(t["symbol"])
                view["bot_id"] = bot["id"]
                view["bot_name"] = bot["name"]
                view["current_price"] = price
                if price:
                    gross = botrunner.execution_pnl(
                        t["direction"], t["entry_price"], price, t["qty"])
                    view["unrealised_pnl"] = round(gross, 2)
                else:
                    view["unrealised_pnl"] = None
                out.append(view)
        self._send_json({"trades": out})

    def api_news(self):
        user, _ = self._require_user()
        if not user:
            return
        if user["role"] != "admin" and user["plan"] not in ("pro",
                                                            "premium"):
            return self._send_json(
                {"error": "The news feed is part of the Pro plan.",
                 "upgrade": True}, 403)
        news, alerts, fg = load_news()
        self._send_json({"news": news, "alerts": alerts,
                         "fear_greed": fg,
                         "note": "Impact labels are rule-based "
                                 "(keyword/category rules, platform v1) - "
                                 "not AI predictions."})

    def api_compare(self):
        user, _ = self._require_user()
        if not user:
            return
        rows = []
        for bot in db.list_bots():
            if not bot_visible(user, bot):
                continue
            s = bot_summary(bot)
            rows.append({"id": s["id"], "name": s["name"],
                         "kind": s["kind"], "status": s["status"],
                         "published": s["published"], **s["metrics"]})
        self._send_json({"bots": rows, "disclaimer": RISK_DISCLAIMER})

    def api_plans(self):
        self._send_json({
            "plans": PLANS, "test_mode": True,
            "test_mode_note": "TEST MODE checkout: no real payment "
                              "provider is connected. Any test card "
                              "number succeeds and nothing is charged.",
            "disclaimer": RISK_DISCLAIMER,
            "autotrading_note": "Auto-trading is NOT available. This "
                                "platform is paper-trading only; no "
                                "exchange keys are collected, ever, in "
                                "this build.",
        })

    # -- live signals (signal-first home page) ------------------------------
    def _signal_view(self, user, card):
        """Apply the plan promise to one signal card: the signal
        itself (action + levels) is visible to every logged-in user;
        the agents' full reasons are the Pro feature the plans page
        already advertises ('Live signals with the agents' reasons')."""
        view = dict(card)
        if user["role"] == "admin" or user["plan"] in ("pro", "premium"):
            view["reasons_visible"] = True
        else:
            view["reasons"] = [
                "The agents' full reasons for this signal are part "
                "of the Pro plan - see Billing & plans. The signal "
                "itself (BUY / SELL / WAIT with entry, target and "
                "stop) is always shown."]
            view["reasons_visible"] = False
        view["disclaimer"] = RISK_DISCLAIMER
        return view

    def _signal_quota(self, user):
        """Free-plan daily quota for single-market signal searches.

        Returns (allowed, info). Admin and Pro/Premium are unlimited
        (info is None). For a Free user under the limit, one search is
        counted and info carries {plan, used, limit} AFTER counting;
        over the limit, nothing is counted and allowed is False."""
        if user["role"] == "admin" or user["plan"] in ("pro", "premium"):
            return True, None
        day = time.strftime("%Y-%m-%d", time.gmtime())
        used = db.signal_usage_count(user["id"], day)
        if used >= FREE_SIGNAL_DAILY_LIMIT:
            return False, {"plan": user["plan"], "used": used,
                           "limit": FREE_SIGNAL_DAILY_LIMIT}
        used = db.signal_usage_bump(user["id"], day)
        return True, {"plan": user["plan"], "used": used,
                      "limit": FREE_SIGNAL_DAILY_LIMIT}

    def api_signal(self):
        user, _ = self._require_user()
        if not user:
            return
        query = parse_qs(urlparse(self.path).query)
        raw = (query.get("symbol") or [""])[0]
        symbol = signals_mod.normalize_symbol(raw)
        if not symbol:
            return self._send_json(
                {"error": "Please give a market symbol, e.g. "
                          "?symbol=BTCUSDT (or just BTC)."}, 400)
        allowed, quota = self._signal_quota(user)
        if not allowed:
            return self._send_json(
                {"error": "You have used today's "
                          f"{quota['limit']} free signal searches. "
                          "Pro gives unlimited live signal searches - "
                          "see Billing & plans.",
                 "quota": quota, "upgrade": True}, 429)
        card = signals_mod.get_signal(symbol)
        view = self._signal_view(user, card)
        if quota is not None:
            view["quota"] = quota
        self._send_json(view)

    def api_signals(self):
        user, _ = self._require_user()
        if not user:
            return
        payload = signals_mod.get_signals()
        self._send_json({
            "signals": [self._signal_view(user, c)
                        for c in payload["signals"]],
            "error": payload["error"],
            "as_of": payload["as_of"],
            "universe_note": payload.get("universe_note"),
            "disclaimer": RISK_DISCLAIMER,
        })

    # -- watchlist ------------------------------------------------------------
    def api_watchlist(self):
        user, _ = self._require_user()
        if not user:
            return
        symbols = db.watchlist_list(user["id"])
        cards = [self._signal_view(user, signals_mod.get_signal(s))
                 for s in symbols]
        self._send_json({"symbols": symbols, "signals": cards,
                         "disclaimer": RISK_DISCLAIMER})

    def api_watchlist_add(self):
        user, _ = self._require_user()
        if not user:
            return
        symbol = signals_mod.normalize_symbol(self._body().get("symbol"))
        if not symbol:
            return self._send_json(
                {"error": "Please give a valid market symbol, e.g. "
                          "BTC or BTCUSDT."}, 400)
        db.watchlist_add(user["id"], symbol)
        self._send_json({"ok": True,
                         "symbols": db.watchlist_list(user["id"])})

    def api_watchlist_remove(self):
        user, _ = self._require_user()
        if not user:
            return
        symbol = signals_mod.normalize_symbol(self._body().get("symbol"))
        if not symbol:
            return self._send_json(
                {"error": "Please give a valid market symbol, e.g. "
                          "BTC or BTCUSDT."}, 400)
        db.watchlist_remove(user["id"], symbol)
        self._send_json({"ok": True,
                         "symbols": db.watchlist_list(user["id"])})

    # -- public track record ---------------------------------------------------
    def api_record(self):
        """The latest closed paper trades from PUBLISHED bots only -
        wins and losses alike, never filtered by outcome. This is the
        platform's public proof: if a published bot lost, it shows."""
        user, _ = self._require_user()
        if not user:
            return
        trades = []
        for bot in db.list_bots(published_only=True):
            for t in read_bot_trades(bot):
                if t["status"] != "closed":
                    continue
                trades.append({
                    "bot_id": bot["id"], "bot_name": bot["name"],
                    "symbol": t["symbol"], "direction": t["direction"],
                    "entry_price": t["entry_price"],
                    "exit_price": t.get("exit_price"),
                    "pnl": t.get("pnl"),
                    "exit_reason": t.get("exit_reason"),
                    "exit_ts_ms": t.get("exit_ts_ms"),
                })
        trades.sort(key=lambda x: x["exit_ts_ms"] or 0, reverse=True)
        self._send_json({"trades": trades[:50],
                         "disclaimer": RISK_DISCLAIMER})

    def api_admin_users(self):
        user, _ = self._require_admin()
        if user:
            self._send_json({"users": db.list_users()})

    def api_admin_logs(self):
        user, _ = self._require_admin()
        if user:
            self._send_json({"logs": db.list_audit()})

    def api_admin_backup_create(self):
        user, _ = self._require_admin()
        if not user:
            return
        folder, files = backup_service.backup_once(DATA_DIR)
        db.audit(user["id"], "backup_created",
                 f"{os.path.basename(folder)}: {len(files)} files")
        self._send_json({"ok": True,
                         "folder": os.path.basename(folder),
                         "files": files})

    def api_admin_backup_download(self):
        user, _ = self._require_admin()
        if not user:
            return
        filename, blob = backup_service.latest_backup_zip(DATA_DIR)
        db.audit(user["id"], "backup_downloaded", filename)
        self._send_bytes(blob, "application/zip", filename)

    # -- write APIs -----------------------------------------------------------
    def api_create_bot(self):
        user, _ = self._require_admin()
        if not user:
            return
        body = self._body()
        kind = body.get("kind", "v1")
        if kind not in ("v1", "v2", "event", "breakout", "router",
                        "range"):
            return self._send_json({"error": "Unknown bot kind."}, 400)
        name = (body.get("name") or "").strip() or f"{kind.upper()} bot"
        symbols = body.get("symbols") or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        universe_mode = body.get("universe_mode") or "fixed"
        if universe_mode not in ("fixed", "screener"):
            return self._send_json(
                {"error": "universe_mode must be 'fixed' or 'screener'."},
                400)
        try:
            universe_size = int(body.get("universe_size") or 20)
        except (TypeError, ValueError):
            universe_size = 0
        if not (5 <= universe_size <= 30):
            return self._send_json(
                {"error": "universe_size must be between 5 and 30."}, 400)
        if kind == "event":
            symbols = []
            universe_mode = "fixed"  # the Event Watcher does not trade
        bot = db.create_bot(
            name=name, kind=kind,
            start_equity=float(body.get("start_equity") or 10000),
            symbols=symbols,
            risk_pct=float(body.get("risk_pct") or 0.01),
            daily_loss_pct=float(body.get("daily_loss_pct") or 0.03),
            cycle_minutes=int(body.get("cycle_minutes") or 0),
            created_by=user["id"],
            universe_mode=universe_mode, universe_size=universe_size)
        db.update_bot(bot["id"], status="paused")
        botrunner.init_bot_journal(db.get_bot(bot["id"]))
        db.audit(user["id"], "bot_created", f"#{bot['id']} {name} ({kind})")
        self._send_json({"bot": bot_summary(db.get_bot(bot["id"]))})

    def api_bot_action(self, bot_id, action):
        user, _ = self._require_user()
        if not user:
            return
        bot = db.get_bot(bot_id)
        if not bot:
            return self._send_json({"error": "Bot not found."}, 404)

        if action == "follow":
            if not bot["published"]:
                return self._send_json({"error": "Bot is not published."},
                                       400)
            db.follow(user["id"], bot_id)
            return self._send_json({"ok": True})

        # Everything below is admin-only.
        if user["role"] != "admin":
            return self._send_json({"error": "Admin only."}, 403)

        if action == "cycle":
            summary = botrunner.run_cycle(db, bot)
            db.audit(user["id"], "bot_cycle", f"#{bot_id}: {summary[:120]}")
            return self._send_json({"ok": True, "summary": summary,
                                    "bot": bot_summary(db.get_bot(bot_id))})
        if action == "start":
            botrunner.clear_bot_halt(bot)
            db.update_bot(bot_id, status="running")
            db.audit(user["id"], "bot_start", f"#{bot_id}")
            return self._send_json({"ok": True,
                                    "bot": bot_summary(db.get_bot(bot_id))})
        if action == "pause":
            db.update_bot(bot_id, status="paused")
            return self._send_json({"ok": True,
                                    "bot": bot_summary(db.get_bot(bot_id))})
        if action == "stop":
            db.update_bot(bot_id, status="stopped")
            return self._send_json({"ok": True,
                                    "bot": bot_summary(db.get_bot(bot_id))})
        if action == "publish":
            db.update_bot(bot_id, published=1)
            db.audit(user["id"], "bot_publish", f"#{bot_id}")
            return self._send_json({"ok": True,
                                    "bot": bot_summary(db.get_bot(bot_id))})
        if action == "unpublish":
            db.update_bot(bot_id, published=0)
            return self._send_json({"ok": True,
                                    "bot": bot_summary(db.get_bot(bot_id))})
        if action == "kill":
            closed = botrunner.kill_bot(db, bot, by=user["email"])
            db.audit(user["id"], "bot_kill",
                     f"#{bot_id}: closed {len(closed)}")
            return self._send_json({"ok": True, "closed": closed,
                                    "bot": bot_summary(db.get_bot(bot_id))})
        if action == "demo-trade":
            if bot["kind"] == "event":
                return self._send_json(
                    {"error": "The Event Watcher does not trade."}, 400)
            if bot["kind"] == "breakout":
                return self._send_json(
                    {"error": "Test positions use the analyst agents. "
                              "The Breakout bot only trades its own "
                              "breakout signals, so it has no test "
                              "position."}, 400)
            if bot["kind"] == "router":
                return self._send_json(
                    {"error": "Test positions use the analyst agents "
                              "directly. The Regime Router only trades "
                              "what its routing rules select - a "
                              "situation matched to a proven specialist "
                              "- so it has no test position."}, 400)
            if bot["kind"] == "range":
                return self._send_json(
                    {"error": "Test positions use the analyst agents. "
                              "The Range Specialist candidate only "
                              "trades its own gated signals - its "
                              "forward record must stay clean - so it "
                              "has no test position."}, 400)
            result = botrunner.open_demo_position(db, bot)
            db.audit(user["id"], "bot_demo_trade",
                     f"#{bot_id}: {result.get('detail', '')[:120]}")
            return self._send_json(result,
                                   status=200 if result.get("ok") else 400)
        if action == "settings":
            body = self._body()
            fields = {}
            if "symbols" in body and isinstance(body["symbols"], list):
                fields["symbols"] = json.dumps(body["symbols"])
            for key in ("risk_pct", "daily_loss_pct"):
                if key in body:
                    val = float(body[key])
                    if not (0 < val <= 0.25):
                        return self._send_json(
                            {"error": f"{key} must be between 0 and 0.25."},
                            400)
                    fields[key] = val
            if "cycle_minutes" in body:
                fields["cycle_minutes"] = int(body["cycle_minutes"])
            if "universe_mode" in body:
                if body["universe_mode"] not in ("fixed", "screener"):
                    return self._send_json(
                        {"error": "universe_mode must be 'fixed' or "
                                  "'screener'."}, 400)
                fields["universe_mode"] = body["universe_mode"]
            if "universe_size" in body:
                try:
                    usize = int(body["universe_size"])
                except (TypeError, ValueError):
                    usize = 0
                if not (5 <= usize <= 30):
                    return self._send_json(
                        {"error": "universe_size must be between 5 "
                                  "and 30."}, 400)
                fields["universe_size"] = usize
            if "name" in body and body["name"]:
                fields["name"] = str(body["name"])[:80]
            db.update_bot(bot_id, **fields)
            db.audit(user["id"], "bot_settings",
                     f"#{bot_id}: {list(fields)}")
            return self._send_json({"ok": True,
                                    "bot": bot_summary(db.get_bot(bot_id))})
        return self._send_json({"error": "Unknown action."}, 404)

    def api_close_trade(self, bot_id, trade_id):
        user, _ = self._require_admin()
        if not user:
            return
        bot = db.get_bot(bot_id)
        if not bot:
            return self._send_json({"error": "Bot not found."}, 404)
        result = botrunner.close_position(
            db, bot, trade_id, "manual_close",
            f"Closed manually by {user['email']} from the dashboard.")
        db.audit(user["id"], "trade_manual_close",
                 f"bot #{bot_id} trade #{trade_id}: {result}")
        self._send_json(result, status=200 if result.get("ok") else 400)

    def api_checkout(self):
        user, _ = self._require_user()
        if not user:
            return
        body = self._body()
        plan = body.get("plan")
        if plan not in ("pro", "premium"):
            return self._send_json({"error": "Choose Pro or Premium."}, 400)
        card = "".join(ch for ch in str(body.get("card_number") or "")
                       if ch.isdigit())
        if not (12 <= len(card) <= 19):
            return self._send_json(
                {"error": "Please enter a test card number (12-19 "
                          "digits). TEST MODE: any number works, "
                          "nothing is charged."}, 400)
        amount = PLANS[plan]["price"]
        db.record_payment(user["id"], plan, amount, card[-4:])
        db.set_plan(user["id"], plan)
        db.audit(user["id"], "checkout_test",
                 f"plan={plan} amount=${amount} card=****{card[-4:]} "
                 "(TEST MODE)")
        self._send_json({
            "ok": True, "test_mode": True,
            "message": f"TEST MODE payment succeeded - ${amount} "
                       f"simulated charge on card ****{card[-4:]}. "
                       f"Your {PLANS[plan]['name']} plan is now active. "
                       "No real money was charged.",
            "user": public_user(db.get_user(user["id"])),
        })

    def api_kill_global(self):
        user, _ = self._require_admin()
        if not user:
            return
        closed = botrunner.global_kill(db, by=user["email"])
        db.audit(user["id"], "global_kill", f"closed {len(closed)}")
        self._send_json({"ok": True, "closed_count": len(closed)})

    def api_kill_global_clear(self):
        user, _ = self._require_admin()
        if not user:
            return
        botrunner.global_clear(db)
        db.audit(user["id"], "global_kill_clear", "")
        self._send_json({"ok": True})


def main():
    stop_event = threading.Event()
    scheduler = threading.Thread(target=botrunner.scheduler_loop,
                                 args=(db, stop_event), daemon=True)
    scheduler.start()
    backups = threading.Thread(target=backup_service.backup_loop,
                               args=(DATA_DIR, stop_event), daemon=True)
    backups.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"AlphaSwarm platform running at http://127.0.0.1:{PORT} "
          f"(PAPER TRADING ONLY, billing in TEST MODE)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_event.set()


if __name__ == "__main__":
    main()
