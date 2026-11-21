"""AlphaSwarm platform engine - central configuration.

Copied from the AlphaPaper v3 research branch and adapted for the
platform (per-bot values such as SYMBOLS, STRATEGY, START_EQUITY,
RISK_PER_TRADE and MAX_DAILY_LOSS are patched in by botrunner.py for the
duration of each bot cycle, under a global engine lock).

SAFETY: MODE is hard-coded to "paper". There is no live-trading code
path in this project at all: no exchange accounts, no API keys, no real
orders. The execution module is a simulator that only writes to a local
SQLite journal. The risk policy refuses to approve anything if
MODE != "paper".
"""

MODE = "paper"  # hard-coded safety switch - paper simulation only

# Strategy selection: "v1" = the original rules; "v2" = v1 plus the two
# diagnosis-driven filters in strategy_v2.py (hard 4h trend filter +
# Momentum agreement required). botrunner patches this per bot.
STRATEGY = "v1"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
PRIMARY_INTERVAL = "1h"
HTF_INTERVAL = "4h"  # higher-timeframe context for the trend agent
CANDLE_LIMIT = 300

START_EQUITY = 10_000.0  # paper USDT (patched per bot)

# Risk policy parameters (see policy.py)
RISK_PER_TRADE = 0.01        # max 1% of equity risked per trade (per-bot)
MAX_OPEN_POSITIONS = 2       # across all symbols
MAX_DAILY_LOSS = 0.03        # halt for the day at -3% of day-start equity
SIM_LEVERAGE = 3.0           # fixed simulated leverage cap for sizing display
MAX_DATA_AGE_INTERVALS = 2.0  # data older than 2 candle intervals is stale

# Decision parameters (see decision.py) - one sensible default set, not tuned
MIN_AGENT_AGREEMENT = 3      # at least 3 of 4 analyst agents
MIN_AVG_CONFIDENCE = 0.60    # average confidence of the agreeing agents
ATR_SL_MULTIPLIER = 1.5      # fallback stop distance when no S/R level is near
TP_R_MULTIPLE = 2.0          # take profit at 2x the stop distance

# ---- Platform execution upgrades (NOT in the frozen forward-run copies)
# Paper fills pay a realistic taker-style fee on entry and on every exit
# (partial exits included). 0.1% per side is the standard Binance spot
# taker fee level; it is charged on notional.
FEE_RATE = 0.001             # 0.1% per side, on notional
# Partial profit booking: when price reaches TP1 (1R), this fraction of
# the position is closed and the stop moves to break-even.
TP1_R_MULTIPLE = 1.0         # first target at 1x the stop distance
PARTIAL_TP_FRACTION = 0.5    # close 50% of the position at TP1
# After TP1 is booked, the stop trails the best price seen by one risk
# distance (1R) - the classic trailing stop.
TRAILING_ENABLED = True

BINANCE_HOSTS = [
    "https://data-api.binance.vision",  # Binance public market-data host
    "https://api.binance.com",          # main public API (may be geo-blocked)
]

DB_PATH = "alpha_paper.db"
DASHBOARD_PATH = "dashboard.html"
HALT_FILE = "HALT"  # if this file exists in the bot dir, that bot halts
HALT_ENV = "ALPHAPAPER_HALT"

# ---- All-coin screener + news/event agent (from the v3 branch) --------
# botrunner patches the *_PATH values to absolute paths inside the
# platform data directory before running an Event Watcher cycle.

# Stage A - screener (see screener.py)
SCREENER_MIN_QUOTE_VOLUME = 5_000_000.0  # min 24h quote volume, USDT
SCREENER_MAX_ABS_CHANGE_PCT = 60.0       # sanity bound on |24h % move|
SCREENER_TOP_N = 20
SCREENER_REPORT_PATH = "screener_report.md"
SCREENER_UNIVERSE_PATH = "screener_universe.json"

# Stage B - events (see events.py)
EVENT_LOG_PATH = "events.jsonl"          # point-in-time event log (JSONL)
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
BINANCE_CMS_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/"
                   "article/list/query?type=1&pageNo=1&pageSize=20")
RSS_FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
}
# Binance announcement catalogs worth watching: 48 = new listings,
# 161 = delistings, 49 = latest news.
BINANCE_WATCH_CATALOGS = {48, 49, 161}

# Stage C - event-driven paper rules (see event_rules.py; the platform's
# Event Watcher bot is non-trading, these are kept for parity with v3)
EVENT_SLIPPAGE = 0.02                    # +2% on event-driven entries
EVENT_ATR_SL_MULTIPLIER = 1.5            # stop distance = 1.5 x ATR(14)
EVENT_TP_R_MULTIPLE = 2.0                # take profit at 2R, as in v1/v2
EVENT_MAX_NEWS_AGE_MS = 3 * 3_600_000

# Confirmation layer (see events.assign_confirmations / alerts.py):
# CONFIRMED = official source, or >= 2 independent credible sources
# reporting the same category + primary symbol within this window.
EVENT_CONFIRM_WINDOW_MS = 24 * 3_600_000
EVENT_ALERT_MAX_AGE_MS = 48 * 3_600_000
ALERTS_LOG_PATH = "alerts.jsonl"
