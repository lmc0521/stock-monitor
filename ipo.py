"""
IPO calendar from Nasdaq's (unofficial) endpoint.

Three sections per month: priced (already IPO'd), upcoming (expected to price),
and filed (registered with the SEC). Parsing is pure and unit-tested; only
fetch_calendar touches the network. Nasdaq 403s without browser-like headers.
"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}
BASE_URL = 'https://api.nasdaq.com/api/ipo/calendar?date='


def _get_json(url: str):
    with urlopen(Request(url, headers=_HEADERS), timeout=20) as r:
        return json.load(r)


def _norm(row: dict, date_field: str) -> dict:
    return {
        'symbol':   (row.get('proposedTickerSymbol') or '').strip(),
        'company':  (row.get('companyName') or '').strip(),
        'exchange': (row.get('proposedExchange') or '').strip(),
        'price':    (row.get('proposedSharePrice') or '').strip(),
        'shares':   (row.get('sharesOffered') or '').strip(),
        'date':     (row.get(date_field) or '').strip(),
        'amount':   (row.get('dollarValueOfSharesOffered') or '').strip(),
    }


def parse_calendar(payload: dict) -> dict:
    """Normalize Nasdaq's calendar payload into priced/upcoming/filed lists."""
    data = payload.get('data') or {}

    def rows(section: str) -> list:
        node = data.get(section) or {}
        r = node.get('rows')
        if r is None:                                  # upcoming sometimes nests
            r = (node.get('upcomingTable') or {}).get('rows')
        return r or []

    return {
        'priced':   [_norm(r, 'pricedDate')        for r in rows('priced')],
        'upcoming': [_norm(r, 'expectedPriceDate')  for r in rows('upcoming')],
        'filed':    [_norm(r, 'filedDate')          for r in rows('filed')],
        'month':    data.get('month'),
        'year':     data.get('year'),
    }


def fetch_calendar(month: str) -> dict:
    """Fetch the IPO calendar for a given month ('YYYY-MM')."""
    import data as _data
    try:
        out = parse_calendar(_get_json(BASE_URL + month))
        _data.report_health('Nasdaq IPO', True)
        return out
    except Exception as exc:
        _data.report_health('Nasdaq IPO', False, str(exc))
        raise
