"""
Technical-indicator math. Pure functions over a pandas Close series so they can
be unit-tested without any charting or network.
"""

from __future__ import annotations


def sma(series, n: int):
    """Simple moving average."""
    return series.rolling(n).mean()


def bollinger(close, n: int = 20, k: float = 2.0):
    """Bollinger Bands: (middle SMA, upper, lower) = SMA ± k * rolling std."""
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def rsi(close, n: int = 14):
    """Relative Strength Index (Wilder's smoothing), 0–100."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD: (macd line, signal line, histogram)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig
