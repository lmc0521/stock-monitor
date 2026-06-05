"""App state + model layer (no Qt): file paths, portfolio P&L, alerts."""

import os
import sys
import json
import csv

def _app_dir() -> str:
    """Directory for read/write state — next to the .exe when frozen, else the source dir."""
    if getattr(sys, 'frozen', False):           # running as a PyInstaller bundle
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


_APP_DIR = _app_dir()
WATCHLIST_FILE = os.path.join(_APP_DIR, 'watchlist.json')
PORTFOLIO_FILE = os.path.join(_APP_DIR, 'portfolio.json')
ALERTS_FILE    = os.path.join(_APP_DIR, 'alerts.json')

def parse_portfolio_csv(path: str):
    """
    Parse a portfolio CSV with columns: symbol, shares, avg_cost.
    A row whose symbol is CASH puts its 'shares' column into the cash balance.
    A header row (symbol/ticker) is skipped. Returns (holdings, cash).
    """
    holdings, cash = [], 0.0
    with open(path, newline='') as f:
        for raw in csv.reader(f):
            if not raw or not raw[0].strip():
                continue
            sym = raw[0].strip()
            if sym.lower() in ('symbol', 'ticker'):       # header
                continue
            try:
                if sym.upper() == 'CASH':
                    cash += float(raw[1])
                    continue
                shares  = float(raw[1])
                avg_cost = float(raw[2])
            except (IndexError, ValueError):
                continue
            holding = {'symbol': sym.upper(), 'shares': shares, 'avg_cost': avg_cost}
            # optional 4th column: buy date YYYY-MM-DD (for history reconstruction)
            if len(raw) > 3 and raw[3].strip():
                try:
                    from datetime import datetime as _dt
                    holding['date'] = _dt.strptime(raw[3].strip(), '%Y-%m-%d').date().isoformat()
                except ValueError:
                    pass
            holdings.append(holding)
    return holdings, cash


def compute_portfolio(holdings: list, prices: dict, cash: float = 0.0) -> dict:
    """
    Compute per-holding and total P&L.
    holdings: [{'symbol','shares','avg_cost'}]; prices: {symbol: current_price}.
    Holdings with no known price are listed but excluded from market totals.
    """
    rows = []
    invested_cost = 0.0       # cost of all positions (incl. those missing a price)
    market_value  = 0.0       # market value of priced positions only
    priced_cost   = 0.0       # cost of priced positions only (for % return)

    for h in holdings:
        shares, avg = float(h['shares']), float(h['avg_cost'])
        cost  = shares * avg
        price = prices.get(h['symbol'])
        invested_cost += cost

        if price is None:
            rows.append({**h, 'shares': shares, 'avg_cost': avg, 'cost': cost,
                         'price': None, 'mkt': None, 'pnl': None, 'pnl_pct': None})
            continue

        mkt = shares * price
        pnl = mkt - cost
        market_value += mkt
        priced_cost  += cost
        rows.append({**h, 'shares': shares, 'avg_cost': avg, 'cost': cost,
                     'price': price, 'mkt': mkt, 'pnl': pnl,
                     'pnl_pct': (pnl / cost * 100.0) if cost else 0.0})

    total_pnl     = market_value - priced_cost
    total_pnl_pct = (total_pnl / priced_cost * 100.0) if priced_cost else 0.0
    return {
        'rows': rows,
        'cash': cash,
        'invested_cost': invested_cost,
        'market_value': market_value,
        'total_value': market_value + cash,
        'total_pnl': total_pnl,
        'total_pnl_pct': total_pnl_pct,
    }


def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                d = json.load(f)
            return d.get('holdings', []), float(d.get('cash', 0.0))
        except Exception:
            pass
    return [], 0.0


def save_portfolio(holdings: list, cash: float):
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump({'holdings': holdings, 'cash': cash}, f, indent=2)


# ── price alerts (SCADA-style alarms) ─────────────────────────────────────────

def load_alerts() -> list:
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_alerts(alerts: list):
    with open(ALERTS_FILE, 'w') as f:
        json.dump(alerts, f, indent=2)


def evaluate_alerts(alerts: list, symbol: str, price: float) -> list:
    """
    Check a fresh price against the alerts for `symbol`. Returns the list of alerts
    that newly fired this call, and marks them 'triggered' so they don't re-fire
    until re-armed. Pure logic — no I/O.

    Each alert: {'symbol', 'condition': 'above'|'below', 'price', 'enabled', 'triggered'}
    """
    fired = []
    for a in alerts:
        if a.get('symbol') != symbol or not a.get('enabled', True) or a.get('triggered'):
            continue
        cond, thr = a.get('condition'), a.get('price')
        if (cond == 'above' and price >= thr) or (cond == 'below' and price <= thr):
            a['triggered'] = True
            fired.append(a)
    return fired


# ── workers ───────────────────────────────────────────────────────────────────
