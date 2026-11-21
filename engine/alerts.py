"""Alert generator (v3) - CONFIRMED major events only.

For every event that is CONFIRMED (see events.assign_confirmations) and
in a major category, write ONE plain-language alert record to
alerts.jsonl: what happened, which coin, source(s), and time.

Hard rules:
  - UNCONFIRMED events (rumors) NEVER produce an alert.
  - Minor categories (MACRO, REGULATION, OTHER) never alert in this
    stage - they are context, not alarms.
  - Each event alerts at most once (deduplicated by event_id against
    the existing alerts log).
  - An event older than config.EVENT_ALERT_MAX_AGE_MS at processing
    time is not alerted - it is history, not news. (In steady hourly
    polling, alerts are written in the same poll that confirms them.)

An alert is a log record for the owner to read; nothing here sends
messages anywhere, and alerts are labelled with their confirmation
basis so a reader can see WHY the system believes the event is real.
"""

import json
import os
import time

import config

MAJOR_CATEGORIES = {"EXCHANGE_LISTING", "DELISTING", "HACK_OR_EXPLOIT",
                    "ETF_OR_INSTITUTIONAL"}

_WHAT = {
    "EXCHANGE_LISTING": "New exchange listing",
    "DELISTING": "Exchange delisting",
    "HACK_OR_EXPLOIT": "Hack / exploit reported",
    "ETF_OR_INSTITUTIONAL": "ETF / institutional development",
}


def alert_text(event):
    when_ms = event.get("published_ts_ms") or event.get("fetched_ts_ms", 0)
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(when_ms / 1000))
    coins = ", ".join(event.get("symbols") or []) \
        or "not named in the headline"
    sources = ", ".join(event.get("corroborating_sources")
                        or [event.get("source", "?")])
    return (f"{_WHAT.get(event['category'], event['category'])}: "
            f"{event['headline']} | Coin: {coins} | Source(s): {sources} "
            f"| Published: {when} | Status: CONFIRMED "
            f"({event.get('confirmation_basis', '?')})")


def load_alerted_ids(path=None):
    path = path or config.ALERTS_LOG_PATH
    ids = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    ids.add(json.loads(line)["event_id"])
                except (ValueError, KeyError):
                    continue
    return ids


def process_alerts(pool, path=None, now_ms=None):
    """Write alerts for newly CONFIRMED major events in `pool`.
    Returns the list of alert records written (possibly empty)."""
    path = path or config.ALERTS_LOG_PATH
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    already = load_alerted_ids(path)
    written = []
    for ev in pool:
        if ev.get("confirmation") != "CONFIRMED":
            continue  # rumors never alert
        if ev.get("category") not in MAJOR_CATEGORIES:
            continue
        if ev["event_id"] in already:
            continue
        published = ev.get("published_ts_ms") or ev.get("fetched_ts_ms", 0)
        if now_ms - published > config.EVENT_ALERT_MAX_AGE_MS:
            continue  # history, not news
        rec = {"alert_ts_ms": now_ms,
               "event_id": ev["event_id"],
               "category": ev["category"],
               "symbols": ev.get("symbols", []),
               "sources": ev.get("corroborating_sources")
               or [ev.get("source")],
               "confirmation_basis": ev.get("confirmation_basis"),
               "text": alert_text(ev)}
        written.append(rec)
        already.add(ev["event_id"])
    if written:
        with open(path, "a", encoding="utf-8") as f:
            for rec in written:
                f.write(json.dumps(rec) + "\n")
    return written
