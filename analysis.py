"""
Per-stock analysis: analyst price targets (Yahoo) and computed technical
reference levels (support/resistance/MAs/Bollinger).

The technical levels are deterministic reference points — NOT buy/sell advice.
Parsing/level math are pure (unit-tested); fetch_analysis touches the network.
"""

from __future__ import annotations

import data
import indicators as ta


def parse_target(info: dict) -> dict:
    """Extract analyst price-target fields from a yfinance .info dict."""
    cur = info.get('currentPrice') or info.get('regularMarketPrice')
    mean = info.get('targetMeanPrice')
    out = {
        'current':    float(cur) if cur else None,
        'mean':       float(mean) if mean else None,
        'high':       _f(info.get('targetHighPrice')),
        'low':        _f(info.get('targetLowPrice')),
        'median':     _f(info.get('targetMedianPrice')),
        'n_analysts': info.get('numberOfAnalystOpinions'),
        'rec_key':    info.get('recommendationKey'),
        'rec_mean':   _f(info.get('recommendationMean')),
    }
    out['upside_pct'] = ((out['mean'] - out['current']) / out['current'] * 100.0
                         if out['mean'] and out['current'] else None)
    return out


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def technical_levels(df, info: dict | None = None) -> dict:
    """
    Compute reference levels around the current price. Pure function.

    Returns {current, levels:[{name, value, pct}]} sorted high→low. Levels above
    the current price act as resistance (potential sell reference); below as
    support (potential buy reference).
    """
    info = info or {}
    close = df['Close'].dropna()
    if len(close) == 0:
        return {'current': None, 'levels': []}
    cur = float(close.iloc[-1])
    recent = df.tail(60)
    mid, up, lo = ta.bollinger(close, n=20)

    def last_valid(series):
        s = series.dropna()
        return float(s.iloc[-1]) if len(s) else None

    candidates = [
        ('52-week high',         _f(info.get('fiftyTwoWeekHigh'))),
        ('Recent high (60d)',    float(recent['High'].max()) if len(recent) else None),
        ('Bollinger upper (20)', last_valid(up)),
        ('200-day MA',           _f(info.get('twoHundredDayAverage'))),
        ('50-day MA',            _f(info.get('fiftyDayAverage'))),
        ('Bollinger lower (20)', last_valid(lo)),
        ('Recent low (60d)',     float(recent['Low'].min()) if len(recent) else None),
        ('52-week low',          _f(info.get('fiftyTwoWeekLow'))),
    ]
    levels = [{'name': n, 'value': v, 'pct': (v - cur) / cur * 100.0}
              for n, v in candidates if v and v > 0]
    levels.sort(key=lambda x: x['value'], reverse=True)
    return {'current': cur, 'levels': levels}


def fetch_analysis(symbol: str) -> dict:
    """Fetch analyst targets + technical levels for a symbol."""
    import yfinance as yf
    info = yf.Ticker(symbol).info or {}
    target = parse_target(info)
    try:
        df = data.get_history(symbol, '6mo', '1d')
    except Exception:
        df = None
    if df is not None and not getattr(df, 'empty', True):
        levels = technical_levels(df, info)
    else:
        levels = {'current': target.get('current'), 'levels': []}
    return {'symbol': symbol, 'target': target, 'levels': levels}
