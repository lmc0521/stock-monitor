"""
Investor-sentiment data: CNN Fear & Greed (+ its components), the crypto
Fear & Greed index, and the VIX volatility gauge.

Parsing and classification are pure functions (unit-tested offline); only the
fetch_* / gather functions touch the network.
"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

import data  # reuse the cached market-data layer for VIX

# CNN's dataviz endpoint 418s without browser-like headers.
_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://edition.cnn.com',
    'Referer': 'https://edition.cnn.com/',
}

CNN_URL    = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'
CRYPTO_URL = 'https://api.alternative.me/fng/?limit=1'

# Friendly names for the CNN component keys we want to show (skips duplicates).
_CNN_COMPONENTS = {
    'market_momentum_sp500': 'Market Momentum (S&P 500)',
    'stock_price_strength':  'Stock Price Strength',
    'stock_price_breadth':   'Stock Price Breadth',
    'put_call_options':      'Put / Call Options',
    'market_volatility_vix': 'Market Volatility (VIX)',
    'junk_bond_demand':      'Junk Bond Demand',
    'safe_haven_demand':     'Safe-Haven Demand',
}


def classify(score: float) -> tuple[str, str]:
    """Map a 0–100 sentiment score to a (label, hex-color) pair."""
    if score is None:
        return ('Unknown', '#888888')
    if score < 25:
        return ('Extreme Fear', '#c0392b')
    if score < 45:
        return ('Fear', '#ef5350')
    if score <= 55:
        return ('Neutral', '#c9a227')
    if score <= 75:
        return ('Greed', '#26a69a')
    return ('Extreme Greed', '#1e8f6f')


def _get_json(url: str):
    with urlopen(Request(url, headers=_HEADERS), timeout=10) as r:
        return json.load(r)


def parse_cnn(d: dict) -> dict:
    """Extract the headline score + components from CNN's graphdata payload."""
    fg = d.get('fear_and_greed', {})
    score = fg.get('score')
    components = []
    for key, label in _CNN_COMPONENTS.items():
        c = d.get(key)
        if isinstance(c, dict) and c.get('score') is not None:
            components.append({'label': label, 'score': float(c['score']),
                               'rating': c.get('rating', '')})
    return {
        'score': None if score is None else float(score),
        'rating': fg.get('rating', ''),
        'updated': fg.get('timestamp', ''),
        'components': components,
    }


def parse_crypto(d: dict) -> dict:
    item = (d.get('data') or [{}])[0]
    val = item.get('value')
    return {
        'score': None if val is None else float(val),
        'rating': item.get('value_classification', ''),
    }


def fetch_cnn_fng() -> dict:
    return parse_cnn(_get_json(CNN_URL))


def fetch_crypto_fng() -> dict:
    return parse_crypto(_get_json(CRYPTO_URL))


def fetch_vix() -> float | None:
    closes = data.get_last_closes('^VIX', days=5)
    return float(closes.iloc[-1]) if len(closes) else None


def vix_mood(vix: float | None) -> tuple[str, str]:
    """Rough VIX interpretation (lower = calm, higher = fear)."""
    if vix is None:
        return ('Unknown', '#888888')
    if vix < 15:
        return ('Calm', '#26a69a')
    if vix < 20:
        return ('Normal', '#c9a227')
    if vix < 30:
        return ('Elevated / Anxious', '#ef5350')
    return ('High Fear', '#c0392b')


def gather() -> dict:
    """Fetch everything, tolerating individual source failures."""
    out = {'cnn': None, 'crypto': None, 'vix': None, 'errors': []}
    for key, fn in (('cnn', fetch_cnn_fng), ('crypto', fetch_crypto_fng), ('vix', fetch_vix)):
        try:
            out[key] = fn()
        except Exception as exc:                      # noqa: BLE001
            out['errors'].append(f'{key}: {exc}')
    return out
