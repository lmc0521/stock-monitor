"""
Cached market-data access layer.

All yfinance calls funnel through here so that:
  - repeated requests for the same symbol/period are served from a short-lived
    in-memory cache (less load on Yahoo, snappier UI, fewer rate-limit hits),
  - the cache is persisted to disk, so a restart doesn't refetch everything and
    stale data can be served even if the source is down at launch,
  - network calls are throttled to a minimum gap (no burst -> fewer 429s), and
  - transient failures (Yahoo throttling / network blips) are retried with backoff
    before giving up.

Also hosts the data-health registry: every fetch (here and in the other source
modules) reports success/failure per source, so the UI can show what's degraded.

Pure-ish: cache/retry/health logic are testable by injecting fakes.
"""

from __future__ import annotations

import os
import sys
import time
import pickle
import threading

import yfinance as yf


def _app_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CACHE_FILE = os.path.join(_app_dir(), 'cache.pkl')

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


# ── throttling: minimum gap between outbound market-data calls ───────────────
MIN_GAP = 0.4                     # seconds between network fetches
_throttle_lock = threading.Lock()
_last_call = [0.0]


def _throttle(now=None, sleep=None):
    """Block until at least MIN_GAP has passed since the previous network call."""
    now = now or time.time
    sleep = sleep or time.sleep
    with _throttle_lock:
        wait = MIN_GAP - (now() - _last_call[0])
        if wait > 0:
            sleep(wait)
        _last_call[0] = now()


# ── data-health registry ─────────────────────────────────────────────────────
_HEALTH: dict[str, dict] = {}
_health_lock = threading.Lock()


def report_health(source: str, ok: bool, error: str = ''):
    with _health_lock:
        _HEALTH[source] = {'ok': ok, 'ts': time.time(), 'error': '' if ok else str(error)[:200]}


def get_health() -> dict:
    with _health_lock:
        return {k: dict(v) for k, v in _HEALTH.items()}


# ── disk persistence ─────────────────────────────────────────────────────────
_SAVE_GAP = 30.0                  # save to disk at most every 30s
_last_save = [0.0]


def save_cache(path: str | None = None, *, force: bool = False):
    """Persist the in-memory caches to disk (throttled unless force=True)."""
    if not force and time.time() - _last_save[0] < _SAVE_GAP:
        return
    path = path or CACHE_FILE
    try:
        with _LOCK:
            payload = {'history': dict(_HISTORY_CACHE), 'since': dict(_SINCE_CACHE)}
        with open(path, 'wb') as f:
            pickle.dump(payload, f)
        _last_save[0] = time.time()
    except Exception:
        pass                       # cache persistence must never break the app


def load_cache(path: str | None = None) -> bool:
    """Load caches from disk (called once at import). Returns True if loaded."""
    path = path or CACHE_FILE
    if not os.path.exists(path):
        return False
    try:
        with open(path, 'rb') as f:
            payload = pickle.load(f)
        with _LOCK:
            _HISTORY_CACHE.update(payload.get('history', {}))
            _SINCE_CACHE.update(payload.get('since', {}))
        return True
    except Exception:
        return False


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
        _throttle()
        df = _with_retry(lambda: fetcher(symbol, period, interval))
        report_health('Yahoo Finance', True)
    except Exception as exc:
        report_health('Yahoo Finance', False, str(exc))
        if cached:                # serve stale data rather than failing outright
            return cached[1]
        raise

    if df is not None and not getattr(df, 'empty', True):
        with _LOCK:
            _HISTORY_CACHE[key] = (now(), df)
        save_cache()
    return df


def get_last_closes(symbol: str, *, days: int = 5, fetcher=None, now=None):
    """Return the recent daily close series for a symbol (cached via get_history)."""
    df = get_history(symbol, f'{days}d', '1d', fetcher=fetcher, now=now)
    return df['Close'].dropna()


# (symbol, start) -> (timestamp, dataframe), for date-range history
_SINCE_CACHE: dict[tuple, tuple[float, object]] = {}


def get_history_since(symbol: str, start: str, *, ttl: int = 900, fetcher=None, now=None):
    """Daily OHLCV from `start` (YYYY-MM-DD) to today, cached by (symbol, start)."""
    fetcher = fetcher or (lambda s, st: yf.Ticker(s).history(start=st, interval='1d'))
    now = now or time.time
    key = (symbol, str(start))

    with _LOCK:
        cached = _SINCE_CACHE.get(key)
    if cached and (now() - cached[0]) < ttl:
        return cached[1]

    try:
        _throttle()
        df = _with_retry(lambda: fetcher(symbol, start))
        report_health('Yahoo Finance', True)
    except Exception as exc:
        report_health('Yahoo Finance', False, str(exc))
        if cached:
            return cached[1]
        raise

    if df is not None and not getattr(df, 'empty', True):
        with _LOCK:
            _SINCE_CACHE[key] = (now(), df)
        save_cache()
    return df


def clear_cache():
    """Drop all cached data (used by tests and a manual hard-refresh)."""
    with _LOCK:
        _HISTORY_CACHE.clear()
        _SINCE_CACHE.clear()
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
    except Exception:
        pass


# load any previously persisted cache once, at import (after caches are defined)
load_cache()
