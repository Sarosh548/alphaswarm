"""Market data: Binance PUBLIC klines (no API key, read-only).

Tries each configured public host in turn. On any failure (network error,
geo-restriction, bad payload) it falls back to the bundled synthetic sample
dataset so the bot and its tests still run offline. The returned `source`
says honestly which one was used: "live" or "sample".
"""

import http.client
import json
import urllib.request
import urllib.error

import config
from sample_data import sample_candles

_INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000}


def interval_ms(interval: str) -> int:
    return _INTERVAL_MS[interval]


def _fetch_from(host: str, symbol: str, interval: str, limit: int):
    url = (f"{host}/api/v3/klines?symbol={symbol}"
           f"&interval={interval}&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": "AlphaPaperBot/0.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"unexpected payload from {host}")
    candles = []
    for k in raw:
        candles.append({
            "ts": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return candles


def get_candles(symbol: str, interval: str, limit: int = None):
    """Return (candles, source). Drops the still-forming last candle."""
    limit = limit or config.CANDLE_LIMIT
    for host in config.BINANCE_HOSTS:
        try:
            candles = _fetch_from(host, symbol, interval, limit + 1)
            return candles[:-1], "live"  # last candle is incomplete
        except (urllib.error.URLError, ValueError, OSError, TimeoutError,
                http.client.HTTPException):
            # HTTPException covers IncompleteRead: a connection cut
            # mid-body is a fetch failure like any other, and the
            # documented behaviour is to fall back, never to crash
            # the cycle.
            continue
    return sample_candles(symbol, interval, limit), "sample"
