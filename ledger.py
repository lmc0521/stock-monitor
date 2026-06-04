"""
Transaction ledger — the source of truth for what you bought, sold, and earned.

A transaction is one of:
  buy      {'date','symbol','type':'buy','shares','price'}
  sell     {'date','symbol','type':'sell','shares','price'}
  dividend {'date','symbol','type':'dividend','amount'}

From the ledger we derive current positions (net shares + average cost),
realized P&L (average-cost method), and dividends received. Pure functions are
unit-tested; only load/save touch disk.
"""

from __future__ import annotations

import os
import sys
import json

TYPES = ('buy', 'sell', 'dividend')


def _app_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


LEDGER_FILE = os.path.join(_app_dir(), 'transactions.json')


# ── pure derivations ─────────────────────────────────────────────────────────

def _sorted(transactions: list) -> list:
    return sorted(transactions, key=lambda t: (t.get('date', ''),))


def positions(transactions: list) -> dict:
    """
    Reduce the ledger to per-symbol state using the average-cost method.

    Returns {symbol: {shares, cost, avg_cost, realized, dividends}}.
    Sells beyond the held quantity are capped (no negative shares).
    """
    by: dict[str, dict] = {}
    for t in _sorted(transactions):
        sym = t.get('symbol', '')
        typ = t.get('type')
        p = by.setdefault(sym, {'shares': 0.0, 'cost': 0.0, 'realized': 0.0, 'dividends': 0.0})
        if typ == 'buy':
            p['shares'] += float(t['shares'])
            p['cost'] += float(t['shares']) * float(t['price'])
        elif typ == 'sell':
            sell_sh = min(float(t['shares']), p['shares'])
            avg = p['cost'] / p['shares'] if p['shares'] > 1e-12 else 0.0
            p['realized'] += sell_sh * (float(t['price']) - avg)
            p['cost'] -= sell_sh * avg
            p['shares'] -= sell_sh
        elif typ == 'dividend':
            p['dividends'] += float(t.get('amount', 0.0))
    for p in by.values():
        p['avg_cost'] = p['cost'] / p['shares'] if p['shares'] > 1e-9 else 0.0
    return by


def current_holdings(transactions: list) -> list:
    """Net open positions as [{'symbol','shares','avg_cost'}] (for portfolio.json)."""
    out = []
    for sym, p in positions(transactions).items():
        if p['shares'] > 1e-9:
            out.append({'symbol': sym,
                        'shares': round(p['shares'], 6),
                        'avg_cost': round(p['avg_cost'], 6)})
    out.sort(key=lambda h: h['symbol'])
    return out


def summary(transactions: list) -> dict:
    """Totals across the ledger: realized P&L, dividends, and open-position count."""
    pos = positions(transactions)
    return {
        'realized': sum(p['realized'] for p in pos.values()),
        'dividends': sum(p['dividends'] for p in pos.values()),
        'open_positions': sum(1 for p in pos.values() if p['shares'] > 1e-9),
        'by_symbol': pos,
    }


# ── persistence ──────────────────────────────────────────────────────────────

def load_transactions() -> list:
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_transactions(transactions: list):
    with open(LEDGER_FILE, 'w') as f:
        json.dump(_sorted(transactions), f, indent=2)
