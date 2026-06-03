"""
Cached market-data access layer.

All yfinance calls funnel through here so that:
  - repeated requests for the same symbol/period are served from a short-lived
    in-memory cache (less load on Yahoo, snappier UI, fewer rate-limit hits), and
  - transient failures (Yahoo throttling / network blips) are retried with backoff
    before giving up.

Pure-ish: the cache and retry logic are testable by injecting a fake fetcher.
"""

from __future__ import annotations

import time
import threading

import yfinance as yf

# (symbol, period, interval) -> (timestamp, dataframe)
_HISTORY_CACHE: dict[tuple, tuple[float, object]] = {}
_LOCK = threading.Lock()

# Default time-to-live per interval. Intraday data changes fast; daily/weekly slow.
DEFAULT_TTL = {
    '5m': 60, '15m': 120, '30m': 120, '60m': 300,
    '1d': 300, '1wk': 600, '1mo': 1800,
}
FALLBACK_TTL = 300


def _ttl_for(interval: str) -> int:
    return DEFAULT_TTL.get(interval, FALLBACK_TTL)


def _with_retry(fn, *, attempts: int = 3, base_delay: float = 0.8):
    """Call fn(), retrying on exception with exponential backoff."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:          # noqa: BLE001 - we genuinely want any failure
            last_exc = exc
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last_exc


def get_history(symbol: str, period: str, interval: str, *,
                ttl: int | None = None, fetcher=None, now=None):
    """
    Return an OHLCV DataFrame for symbol, cached by (symbol, period, interval).

    `fetcher` and `now` are injectable for testing (default: yfinance + time.time).
    Raises if the data cannot be fetched and nothing is cached.
    """
    fetcher = fetcher or (lambda s, p, i: yf.Ticker(s).history(period=p, interval=i))
    now = now or time.time
    ttl = _ttl_for(interval) if ttl is None else ttl
    key = (symbol, period, interval)

    with _LOCK:
        cached = _HISTORY_CACHE.get(key)
    if cached and (now() - cached[0]) < ttl:
        return cached[1]

    try:
        df = _with_retry(lambda: fetcher(symbol, period, interval))
    except Exception:
        if cached:                # serve stale data rather than failing outright
            return cached[1]
        raise

    if df is not None and not getattr(df, 'empty', True):
        with _LOCK:
            _HISTORY_CACHE[key] = (now(), df)
    return df


def get_last_closes(symbol: str, *, days: int = 5, fetcher=None, now=None):
    """Return the recent daily close series for a symbol (cached via get_history)."""
    df = get_history(symbol, f'{days}d', '1d', fetcher=fetcher, now=now)
    return df['Close'].dropna()


def clear_cache():
    """Drop all cached data (used by tests and a manual hard-refresh)."""
    with _LOCK:
        _HISTORY_CACHE.clear()
