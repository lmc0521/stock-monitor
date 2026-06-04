"""
Portfolio value history — hybrid of:
  1. Reconstruction: rebuild your portfolio's value curve from each holding's
     purchase date + historical prices, so you see history "from start to now".
  2. Forward snapshots: record today's total value to history.json each time the
     portfolio is computed, building an exact ongoing record.

The reconstruction/aggregation functions are pure (operate on pandas Series passed
in) so they can be unit-tested offline. Only fetch_closes / build_* touch the network.
"""

from __future__ import annotations

import os
import sys
import json
from datetime import date, timedelta

import pandas as pd

import data


def _app_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


HISTORY_FILE = os.path.join(_app_dir(), 'history.json')


# ── pure reconstruction ──────────────────────────────────────────────────────

def reconstruct_series(holdings: list, closes_by_symbol: dict, cash: float = 0.0):
    """
    Build the portfolio's total-value time series.

    holdings: [{'symbol','shares','avg_cost','date'?}]; a holding contributes
    shares*close only on/after its 'date' (if given). closes_by_symbol maps symbol
    -> pandas Series of daily closes (naive DatetimeIndex). Returns a pandas Series.
    """
    cols = {}
    for i, h in enumerate(holdings):
        s = closes_by_symbol.get(h['symbol'])
        if s is None or len(s) == 0:
            continue
        contrib = s.astype(float) * float(h['shares'])
        pdate = h.get('date')
        if pdate:
            contrib = contrib[contrib.index >= pd.Timestamp(pdate)]
        if len(contrib):
            cols[f'h{i}'] = contrib

    if not cols:
        return pd.Series(dtype=float)

    df = pd.DataFrame(cols).sort_index().ffill()
    total = df.sum(axis=1) + cash          # skipna: holdings not yet bought are excluded
    return total.dropna()


def invested_series(holdings: list, index, cash: float = 0.0):
    """Cost-basis baseline over `index`: sum of each holding's cost once purchased, + cash."""
    if index is None or len(index) == 0:
        return pd.Series(dtype=float)
    s = pd.Series(cash, index=index, dtype=float)
    for h in holdings:
        cost = float(h['shares']) * float(h['avg_cost'])
        pdate = h.get('date')
        if pdate:
            cutoff = pd.Timestamp(pdate)
            s = s + pd.Series([cost if d >= cutoff else 0.0 for d in index], index=index)
        else:
            s = s + cost
    return s


# ── forward snapshots (history.json) ─────────────────────────────────────────

def load_snapshots() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_snapshots(snaps: list):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(snaps, f, indent=2)


def record_snapshot(total_value, holdings_value, cash, invested_cost, *, when=None) -> list:
    """Record (or replace) today's portfolio snapshot. One entry per calendar day."""
    when = when or date.today().isoformat()
    snaps = [s for s in load_snapshots() if s.get('date') != when]
    snaps.append({
        'date': when,
        'total_value': float(total_value),
        'holdings_value': float(holdings_value),
        'cash': float(cash),
        'invested_cost': float(invested_cost),
    })
    snaps.sort(key=lambda s: s['date'])
    save_snapshots(snaps)
    return snaps


# ── network build pipeline ───────────────────────────────────────────────────

def earliest_start(holdings: list, default_days: int = 365) -> str:
    """Start date for reconstruction: earliest purchase date, else a default lookback."""
    dates = [h['date'] for h in holdings if h.get('date')]
    if dates:
        return min(dates)
    return (date.today() - timedelta(days=default_days)).isoformat()


def fetch_closes(symbols: list, start: str) -> dict:
    """Fetch daily closes since `start` for each symbol (tz stripped for alignment)."""
    out = {}
    for sym in set(symbols):
        try:
            ser = data.get_history_since(sym, start)['Close'].dropna()
            if getattr(ser.index, 'tz', None) is not None:
                ser.index = ser.index.tz_convert(None)
            out[sym] = ser
        except Exception:
            continue
    return out


def build_history(holdings: list, cash: float):
    """
    Return (total_value_series, invested_series) for the portfolio, merging the
    reconstructed curve with any recorded daily snapshots (snapshots win per date).
    """
    if not holdings:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    start = earliest_start(holdings)
    closes = fetch_closes([h['symbol'] for h in holdings], start)
    total = reconstruct_series(holdings, closes, cash)

    # overlay exact recorded snapshots
    snaps = load_snapshots()
    if snaps and len(total):
        for s in snaps:
            try:
                total.loc[pd.Timestamp(s['date'])] = float(s['total_value'])
            except Exception:
                pass
        total = total.sort_index()

    invested = invested_series(holdings, total.index, cash)
    return total, invested
