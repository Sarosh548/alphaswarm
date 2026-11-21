"""Stage B (v3) - news / event agent.

Polls PUBLIC, free, key-less sources and turns raw headlines into TYPED
events with a quarantined keyword extractor. Raw text is never fed into
any decision directly: the only thing downstream code (event_rules.py)
ever sees is the typed record produced here - category, sentiment,
extracted pair symbols, timestamps.

Sources (each failure-tolerant; a dead source is reported, never faked):
  - Binance public announcements CMS (catalog 48 = new listings,
    161 = delistings, 49 = latest news) - the catalog is used only as a
    hint when the headline text itself classifies as OTHER
  - CoinDesk RSS, Cointelegraph RSS
  - Fear & Greed index (api.alternative.me) - sentiment context, fetched
    via get_fear_greed(), not an event source

Point-in-time honesty: every event stores BOTH the publisher timestamp
and the moment THIS system fetched it (fetched_ts_ms). The trading rules
use fetched_ts_ms as the effective time - the bot cannot act on news
before it has seen it. Events are appended to events.jsonl, deduplicated
by a hash of (source, headline, published_ts).

Confirmation layer: every event also carries a confirmation status.
CONFIRMED only when (a) it comes from an OFFICIAL source (the exchange
announcement feed), or (b) at least 2 INDEPENDENT credible sources
report the same event - same category and same primary symbol within
config.EVENT_CONFIRM_WINDOW_MS. Everything else is UNCONFIRMED (a
rumor): logged, but it can never trigger the paper-entry rule
(event_rules.is_trigger requires CONFIRMED) and never produces an
alert labelled as fact (see alerts.py).

Run:  python3 events.py    (one poll of every source)
"""

import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import config

_UA = {"User-Agent": "Mozilla/5.0 (compatible; AlphaPaperV3-Events/0.1)"}

# ---------------------------------------------------------------------------
# Quarantined extractor: headline text -> typed fields. Pure functions.
# ---------------------------------------------------------------------------

# Checked in order; first match wins. Phrases are lowercase substrings.
_CATEGORY_RULES = [
    ("DELISTING", [
        "delist", "removal of", "will remove", "remove trading",
        "removes ", "cease trading", "will cease", "suspend trading pairs",
    ]),
    ("HACK_OR_EXPLOIT", [
        "hack", "exploit", "stolen", "drained", "security breach",
        "breach", "compromised", "rug pull", "scam", "attackers",
        "attacker stole", "looted", "vulnerability exploited",
    ]),
    ("EXCHANGE_LISTING", [
        "will list", "to list ", "lists ", "listing", "listed on",
        "adds ", "add ", "now trading", "now available for trading",
        "spot listing", "will launch", "launches perpetual",
        "perpetual contract", "margin pairs", "trading pairs on",
    ]),
    ("ETF_OR_INSTITUTIONAL", [
        "etf", "blackrock", "fidelity", "institutional", "pension",
        "corporate treasury", "treasury strategy", "asset manager",
        "wealth", "endowment", "sovereign",
    ]),
    ("MACRO", [
        "federal reserve", "fed rate", "interest rate", "rate cut",
        "rate hike", "inflation", "cpi", "jobs report", "unemployment",
        "gdp", "recession", "tariff", "powell", "fomc",
    ]),
    ("REGULATION", [
        "sec ", "regulat", "lawsuit", " sues ", "court", "senate",
        "congress", "lawmaker", "legislat", " ban", "bans ", "banned",
        "legal", "mica", "compliance", "parliament", "regulator",
    ]),
]

# A regulator's "suspicious platforms" list is not an exchange listing.
_LISTING_GUARD_WORDS = ["suspicious", "warning list", "fraud list"]

_NEGATIVE_WORDS = ["reject", "outflow", "sells", "sold", "ban", "sues",
                   "crackdown", "warns", "warning", "probe", "fraud"]
_STRONG_POSITIVE_WORDS = ["approv", "green light", "greenlight",
                          "landmark", "adopts", "adoption", "launches",
                          "launch of", "record inflow", "all-time high"]

# Symbols that are also ordinary English words: never extracted from
# plain headline text (only from $SYM or (SYM) forms).
_COMMON_WORD_SYMBOLS = {
    "ONE", "ALL", "ANY", "CAN", "GET", "HAS", "ITS", "NEW", "NOW",
    "OUT", "PER", "RUN", "SAY", "SEE", "SET", "TOP", "TRY", "TWO",
    "WAY", "WHO", "WHY", "WIN", "YES", "YET", "YOU", "BIG", "LOW",
    "FOR", "THE", "AND", "NOT", "BUT", "ARE", "WAS", "HAD", "HER",
    "HIS", "OUR", "SIX", "TEN", "EAT", "FUN", "HOT", "ICE", "OIL",
    "GOLD", "COIN", "KING", "BABY", "MOON", "STAR", "FISH", "CAKE",
}
# Ordinary words that must not trigger the case-insensitive fallback.
_COMMON_WORDS = {
    "THE", "AND", "FOR", "WITH", "FROM", "THIS", "THAT", "WILL",
    "AFTER", "OVER", "INTO", "SAYS", "SAID", "NEWS", "WEEK", "YEAR",
    "MORE", "THAN", "ABOUT", "THEIR", "FIRST", "COULD", "WOULD",
    "MARKET", "PRICE", "WORLD", "STILL", "UNDER", "ABOVE", "EVERY",
    "OTHER", "BEING", "WHERE", "WHILE", "THOSE", "THESE", "THERE",
    "WHEN", "WHAT", "WHY", "HOW", "ALL", "NEW", "NOW",
}


def classify(headline, source_hint=None):
    """Return (category, sentiment, strong_positive).
    source_hint may be 'binance_listing' / 'binance_delisting' and is
    used ONLY to upgrade an OTHER result from Binance's own catalogs."""
    text = " " + headline.lower() + " "
    category = "OTHER"
    for cat, phrases in _CATEGORY_RULES:
        if any(p in text for p in phrases):
            category = cat
            break
    if category == "OTHER" and source_hint == "binance_listing":
        category = "EXCHANGE_LISTING"
    elif category == "OTHER" and source_hint == "binance_delisting":
        category = "DELISTING"
    if category == "EXCHANGE_LISTING" and \
            any(w in text for w in _LISTING_GUARD_WORDS):
        category = "REGULATION"

    negative = any(w in text for w in _NEGATIVE_WORDS)
    strong = any(w in text for w in _STRONG_POSITIVE_WORDS)
    if category in ("DELISTING", "HACK_OR_EXPLOIT"):
        sentiment = "negative"
    elif category == "EXCHANGE_LISTING":
        sentiment = "positive"
    elif category == "ETF_OR_INSTITUTIONAL":
        sentiment = "negative" if negative else "positive"
    elif category == "REGULATION":
        sentiment = "negative" if negative else "neutral"
    else:
        sentiment = "neutral"
    strong_positive = bool(
        category == "EXCHANGE_LISTING"
        or (sentiment == "positive" and strong
            and category in ("ETF_OR_INSTITUTIONAL", "REGULATION")))
    return category, sentiment, strong_positive


def extract_symbols(headline, universe):
    """universe: {baseAsset: pair}. Returns sorted list of pair symbols
    mentioned in the headline, via $SYM, (SYM), ALL-CAPS word, or a
    length>=4 case-insensitive word match (non-common words only)."""
    found = set()

    def add(base):
        if base in universe:
            found.add(universe[base])

    for m in re.finditer(r"\$([A-Za-z0-9]{2,10})\b", headline):
        add(m.group(1).upper())
    for m in re.finditer(r"\(([A-Za-z0-9]{2,10})\)", headline):
        add(m.group(1).upper())
    tokens = re.findall(r"[A-Za-z0-9]+", headline)
    for tok in tokens:
        up = tok.upper()
        if tok == up and len(tok) >= 2 and up not in _COMMON_WORD_SYMBOLS:
            add(up)  # ALL-CAPS token as printed, e.g. BTC, VELODROME
        elif len(tok) >= 4 and up not in _COMMON_WORDS \
                and up not in _COMMON_WORD_SYMBOLS:
            add(up)  # e.g. 'Ethereum' -> ETH? no: needs exact base match
    return sorted(found)


def make_event(source, headline, url, published_ts_ms, fetched_ts_ms,
               universe, source_hint=None):
    category, sentiment, strong = classify(headline, source_hint)
    eid = hashlib.sha1(
        f"{source}|{headline}|{published_ts_ms}".encode()).hexdigest()[:16]
    return {
        "event_id": eid,
        "source": source,
        "source_hint": source_hint,
        "headline": headline,
        "url": url,
        "published_ts_ms": published_ts_ms,
        "fetched_ts_ms": fetched_ts_ms,
        # the bot may only act once it has SEEN the event:
        "effective_ts_ms": fetched_ts_ms,
        "category": category,
        "sentiment": sentiment,
        "strong_positive": strong,
        "symbols": extract_symbols(headline, universe),
    }


# ---------------------------------------------------------------------------
# Confirmation layer
# ---------------------------------------------------------------------------

# Sources that are the horse's mouth: an exchange's own announcement feed.
OFFICIAL_SOURCES = {"binance_announcements"}
# Sources whose independent agreement can corroborate an event.
CREDIBLE_SOURCES = {"binance_announcements", "coindesk", "cointelegraph"}


def _primary_symbol(event):
    syms = event.get("symbols") or []
    return syms[0] if syms else None


def assign_confirmations(pool, now_ms=None):
    """Set confirmation / confirmation_basis / corroborating_sources on
    every event in `pool` (a list of event dicts), judging each event
    against the whole pool. Pure function of the pool - unit-testable.
    An event keeps its original confirmed_ts_ms once set."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    for ev in pool:
        if ev.get("source") in OFFICIAL_SOURCES:
            ev["confirmation"] = "CONFIRMED"
            ev["confirmation_basis"] = "official_source"
            ev["corroborating_sources"] = [ev["source"]]
        else:
            basis, corr = None, []
            prim = _primary_symbol(ev)
            if prim and ev.get("source") in CREDIBLE_SOURCES:
                for other in pool:
                    if other["event_id"] == ev["event_id"]:
                        continue
                    if other.get("source") == ev.get("source"):
                        continue  # same outlet is not independent
                    if other.get("source") not in CREDIBLE_SOURCES:
                        continue
                    if other.get("category") != ev.get("category"):
                        continue
                    if _primary_symbol(other) != prim:
                        continue
                    if abs(other.get("published_ts_ms", 0)
                           - ev.get("published_ts_ms", 0)) \
                            <= config.EVENT_CONFIRM_WINDOW_MS:
                        basis = "two_independent_sources"
                        corr = sorted({ev["source"], other["source"]})
                        break
            if basis:
                ev["confirmation"] = "CONFIRMED"
                ev["confirmation_basis"] = basis
                ev["corroborating_sources"] = corr
            else:
                ev["confirmation"] = "UNCONFIRMED"
                ev["confirmation_basis"] = "single_source_unconfirmed"
                ev["corroborating_sources"] = []
        if ev["confirmation"] == "CONFIRMED" \
                and not ev.get("confirmed_ts_ms"):
            ev["confirmed_ts_ms"] = now_ms
    return pool


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_binance_announcements():
    """Return list of raw items {headline, url, published_ts_ms, hint}."""
    data = json.loads(_fetch(config.BINANCE_CMS_URL).decode("utf-8"))
    items = []
    for cat in data.get("data", {}).get("catalogs", []):
        cid = cat.get("catalogId")
        if cid not in config.BINANCE_WATCH_CATALOGS:
            continue
        hint = {48: "binance_listing", 161: "binance_delisting"}.get(cid)
        for a in cat.get("articles", []):
            code = a.get("code")
            url = (f"https://www.binance.com/en/support/announcement/"
                   f"detail/{code}" if code else
                   "https://www.binance.com/en/support/announcement")
            items.append({"headline": a.get("title", ""),
                          "url": url,
                          "published_ts_ms": int(a.get("releaseDate", 0)),
                          "hint": hint})
    return items


def fetch_rss(url):
    raw = _fetch(url)
    root = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub = item.findtext("pubDate") or ""
        ts = 0
        if pub:
            try:
                ts = int(parsedate_to_datetime(pub).timestamp() * 1000)
            except (TypeError, ValueError):
                ts = 0
        items.append({"headline": title.strip(), "url": link.strip(),
                      "published_ts_ms": ts, "hint": None})
    return items


_FG_CACHE = {"at": 0.0, "data": None}


def get_fear_greed():
    """Latest Fear & Greed reading, cached 10 minutes, failure-tolerant.
    Returns {value, label, ts_ms} or None."""
    now = time.time()
    if _FG_CACHE["data"] is not None and now - _FG_CACHE["at"] < 600:
        return _FG_CACHE["data"]
    try:
        data = json.loads(_fetch(config.FEAR_GREED_URL, timeout=10)
                          .decode("utf-8"))
        d = data["data"][0]
        out = {"value": int(d["value"]),
               "label": d["value_classification"],
               "ts_ms": int(d["timestamp"]) * 1000}
        _FG_CACHE.update(at=now, data=out)
        return out
    except (urllib.error.URLError, ValueError, OSError, TimeoutError,
            KeyError, IndexError, http.client.HTTPException):
        # HTTPException covers IncompleteRead (connection cut
        # mid-body): fall back to the cached value, as documented.
        return _FG_CACHE["data"]


# ---------------------------------------------------------------------------
# Poll + point-in-time log
# ---------------------------------------------------------------------------

def load_logged_event_ids(path=None):
    path = path or config.EVENT_LOG_PATH
    ids = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    ids.add(json.loads(line)["event_id"])
                except (ValueError, KeyError):
                    continue
    return ids


def load_events(path=None):
    path = path or config.EVENT_LOG_PATH
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def poll_events(universe, path=None):
    """Poll every source once. New events are added to the JSONL log and
    confirmation statuses are (re)assigned across the whole pool - so a
    previously UNCONFIRMED rumor is upgraded if an independent source
    corroborates it later. The log file is rewritten when anything
    changed. Returns (new_events, source_status) where source_status
    maps each source to ('ok', n_items) or ('error', message)."""
    path = path or config.EVENT_LOG_PATH
    fetched = int(time.time() * 1000)
    pool = load_events(path)
    known = {ev["event_id"] for ev in pool}
    before = {ev["event_id"]: ev.get("confirmation") for ev in pool}
    new_events, status = [], {}

    sources = [("binance_announcements", fetch_binance_announcements)]
    for name, url in config.RSS_FEEDS.items():
        sources.append((name, (lambda u: lambda: fetch_rss(u))(url)))

    for name, fn in sources:
        try:
            items = fn()
            status[name] = ("ok", len(items))
            for it in items:
                if not it["headline"]:
                    continue
                ev = make_event(name, it["headline"], it["url"],
                                it["published_ts_ms"] or fetched, fetched,
                                universe, it.get("hint"))
                if ev["event_id"] not in known:
                    known.add(ev["event_id"])
                    pool.append(ev)
                    new_events.append(ev)
        except Exception as exc:  # a dead source is reported, never faked
            status[name] = ("error", f"{type(exc).__name__}: {exc}")

    assign_confirmations(pool, now_ms=fetched)
    changed = bool(new_events) or any(
        ev.get("confirmation") != before.get(ev["event_id"]) for ev in pool)
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            for ev in pool:
                f.write(json.dumps(ev) + "\n")
    return new_events, status


if __name__ == "__main__":
    import alerts
    import screener
    uni = screener.load_universe()
    new, status = poll_events(uni)
    for name, st in status.items():
        print(f"source {name}: {st}")
    fg = get_fear_greed()
    print(f"Fear & Greed: {fg}")
    pool = load_events()
    n_conf = sum(1 for e in pool if e.get("confirmation") == "CONFIRMED")
    print(f"{len(new)} new events logged to {config.EVENT_LOG_PATH} "
          f"({len(pool)} total, {n_conf} CONFIRMED, "
          f"{len(pool) - n_conf} UNCONFIRMED):")
    for ev in new:
        print(f"  [{ev['category']:<20}] "
              f"{ev.get('confirmation', '?'):<11} {ev['symbols']} "
              f"{ev['headline'][:90]}")
    written = alerts.process_alerts(pool)
    print(f"{len(written)} alerts written to {config.ALERTS_LOG_PATH}:")
    for rec in written:
        print(f"  ALERT: {rec['text'][:160]}")
