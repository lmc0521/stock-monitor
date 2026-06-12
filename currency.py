"""
Currency normalization: detect each symbol's trading currency and convert
values to USD so multi-currency portfolios sum correctly.

Symbol->currency mappings are persisted (they never change); FX rates go
through the cached data layer. Pure helpers are unit-tested; only the
detect/rate functions touch the network.
"""

from __future__ import annotations

import os
import sys
import json
import threading

import data


def _app_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CURRENCY_FILE = os.path.join(_app_dir(), 'currencies.json')

_map: dict[str, str] = {}
_loaded = [False]
_lock = threading.Lock()


def _load():
    if _loaded[0]:
        return
    _loaded[0] = True
    if os.path.exists(CURRENCY_FILE):
        try:
            with open(CURRENCY_FILE) as f:
                _map.update(json.load(f))
        except Exception:
            pass


def _save():
    try:
        with open(CURRENCY_FILE, 'w') as f:
            json.dump(_map, f, indent=2)
    except Exception:
        pass


def get_currency(symbol: str, *, detector=None) -> str:
    """Trading currency for a symbol (cached forever; detected via Yahoo once)."""
    with _lock:
        _load()
        if symbol in _map:
            return _map[symbol]

    def _default_detector(sym):
        import yfinance as yf
        return (yf.Ticker(sym).fast_info['currency'] or 'USD').upper()

    detector = detector or _default_detector
    try:
        cur = (detector(symbol) or 'USD').upper()
    except Exception:
        return 'USD'               # assume USD rather than fail the whole page
    with _lock:
        _map[symbol] = cur
        _save()
    return cur


def fx_to_usd(cur: str, *, rate_fetcher=None) -> float:
    """Conversion rate from `cur` to USD (1.0 for USD; 1.0 fallback on failure)."""
    cur = (cur or 'USD').upper()
    if cur == 'USD':
        return 1.0

    def _default_rates(pair):
        closes = data.get_last_closes(pair, days=5)
        return float(closes.iloc[-1]) if len(closes) else None

    rate_fetcher = rate_fetcher or _default_rates
    try:
        rate = rate_fetcher(f'{cur}USD=X')
        return float(rate) if rate else 1.0
    except Exception:
        return 1.0


def fx_for_symbols(symbols: list, *, detector=None, rate_fetcher=None) -> dict:
    """{symbol: (currency, to-USD rate)} for a list of symbols."""
    out = {}
    for sym in symbols:
        cur = get_currency(sym, detector=detector)
        out[sym] = (cur, fx_to_usd(cur, rate_fetcher=rate_fetcher))
    return out
