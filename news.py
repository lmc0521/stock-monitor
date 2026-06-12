"""
Per-stock news headlines via yfinance.

Yahoo's news payload nests the article under 'content' (newer yfinance) or is
flat (older). parse_news handles both and is unit-tested; fetch_news touches
the network.
"""

from __future__ import annotations


def _one(item: dict) -> dict | None:
    c = item.get('content', item) or {}
    title = (c.get('title') or '').strip()
    if not title:
        return None
    url = ''
    canon = c.get('canonicalUrl')
    if isinstance(canon, dict):
        url = canon.get('url') or ''
    if not url:
        click = c.get('clickThroughUrl')
        if isinstance(click, dict):
            url = click.get('url') or ''
    if not url:
        url = c.get('link') or ''
    prov = c.get('provider')
    publisher = (prov.get('displayName') if isinstance(prov, dict) else prov) or \
        c.get('publisher') or ''
    date = (c.get('pubDate') or c.get('displayTime') or '')[:10]
    return {
        'title': title,
        'publisher': publisher,
        'date': date,
        'url': url,
        'summary': (c.get('summary') or c.get('description') or '').strip(),
    }


def parse_news(items: list) -> list:
    """Normalize a yfinance news list (new or old format). Pure function."""
    out = []
    for item in items or []:
        row = _one(item)
        if row:
            out.append(row)
    return out


def fetch_news(symbol: str) -> list:
    """Latest headlines for a symbol."""
    import yfinance as yf
    import data
    try:
        items = yf.Ticker(symbol).news or []
        data.report_health('Yahoo Finance', True)
        return parse_news(items)
    except Exception as exc:
        data.report_health('Yahoo Finance', False, str(exc))
        raise
