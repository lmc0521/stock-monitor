"""
13F institutional holdings from SEC EDGAR.

A 13F-HR filing lists what an institutional manager held at quarter-end. Data is
filed quarterly with a ~45-day delay, so it reflects the most recent filing, not
live positions.

Parsing/aggregation are pure (testable offline); get_holdings touches the network.
SEC requires a descriptive User-Agent with contact info or it returns 403.
"""

from __future__ import annotations

import gzip
import json
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen

_HEADERS = {
    'User-Agent': 'stock-monitor/1.0 (cooker0521@gmail.com)',
    'Accept-Encoding': 'gzip, deflate',
}

# A few well-known managers (name -> CIK). Users can also enter a CIK directly.
KNOWN_FUNDS = {
    'Berkshire Hathaway (Warren Buffett)': '1067983',
    'ARK Invest (Cathie Wood)':            '1697748',
    'Scion Asset Mgmt (Michael Burry)':    '1649339',
    'Pershing Square (Bill Ackman)':       '1336528',
    'Bridgewater Associates':              '1350694',
    'Renaissance Technologies':            '1037389',
    'Citadel Advisors':                    '1423053',
    'Tiger Global Management':             '1167483',
    'BlackRock Inc.':                      '1364742',
}


def _get(url: str) -> bytes:
    with urlopen(Request(url, headers=_HEADERS), timeout=20) as r:
        raw = r.read()
        if r.headers.get('Content-Encoding') == 'gzip':
            raw = gzip.decompress(raw)
        return raw


def _local(tag: str) -> str:
    return tag.split('}')[-1]


def parse_information_table(xml_bytes) -> list:
    """Parse a 13F information-table XML into raw position dicts. Pure function."""
    root = ET.fromstring(xml_bytes)
    rows = []
    for it in root.iter():
        if _local(it.tag) != 'infoTable':
            continue
        d = {'issuer': '', 'class': '', 'cusip': '', 'value': 0, 'shares': 0}
        for ch in it.iter():
            name, text = _local(ch.tag), (ch.text or '').strip()
            if name == 'nameOfIssuer':
                d['issuer'] = text
            elif name == 'titleOfClass':
                d['class'] = text
            elif name == 'cusip':
                d['cusip'] = text
            elif name == 'value':
                d['value'] = int(float(text)) if text else 0
            elif name == 'sshPrnamt':
                d['shares'] = int(float(text)) if text else 0
        if d['issuer']:
            rows.append(d)
    return rows


def aggregate(rows: list) -> list:
    """Combine duplicate positions (same issuer+cusip across sub-managers).

    Returns positions sorted by value desc, each with a 'pct' of total portfolio.
    """
    merged: dict[tuple, dict] = {}
    for r in rows:
        key = (r['issuer'], r['cusip'])
        m = merged.setdefault(key, {'issuer': r['issuer'], 'class': r['class'],
                                    'cusip': r['cusip'], 'value': 0, 'shares': 0})
        m['value'] += r['value']
        m['shares'] += r['shares']

    out = sorted(merged.values(), key=lambda m: m['value'], reverse=True)
    total = sum(m['value'] for m in out) or 1
    for m in out:
        m['pct'] = m['value'] / total * 100.0
    return out


def _normalize_cik(cik: str) -> str:
    digits = ''.join(c for c in str(cik) if c.isdigit())
    return digits.lstrip('0') or '0'


def find_latest_13f(submissions: dict):
    """Return (accession, report_date, filing_date) of the most recent 13F-HR."""
    rec = submissions['filings']['recent']
    for i, form in enumerate(rec['form']):
        if form in ('13F-HR', '13F-HR/A'):
            return rec['accessionNumber'][i], rec['reportDate'][i], rec['filingDate'][i]
    return None


def get_holdings(cik: str) -> dict:
    """Fetch and aggregate the latest 13F holdings for a manager by CIK."""
    import data as _data
    try:
        out = _get_holdings(cik)
        _data.report_health('SEC EDGAR', True)
        return out
    except Exception as exc:
        _data.report_health('SEC EDGAR', False, str(exc))
        raise


def _get_holdings(cik: str) -> dict:
    cik = _normalize_cik(cik)
    submissions = json.loads(_get(f'https://data.sec.gov/submissions/CIK{int(cik):010d}.json'))
    fund_name = submissions.get('name', cik)

    latest = find_latest_13f(submissions)
    if not latest:
        raise ValueError(f'No 13F filings found for {fund_name} (CIK {cik}).')
    accession, report_date, filing_date = latest
    acc_nodash = accession.replace('-', '')

    index = json.loads(_get(
        f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/index.json'))
    xmls = [it['name'] for it in index['directory']['item']
            if it['name'].lower().endswith('.xml') and it['name'].lower() != 'primary_doc.xml']
    if not xmls:
        raise ValueError('Could not locate the holdings table in the filing.')

    # pick the info-table xml (the one that actually contains infoTable elements)
    rows = []
    for name in xmls:
        xml = _get(f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{name}')
        if b'infoTable' in xml or b'informationTable' in xml:
            rows = parse_information_table(xml)
            if rows:
                break

    holdings = aggregate(rows)
    return {
        'fund': fund_name,
        'cik': cik,
        'report_date': report_date,
        'filing_date': filing_date,
        'total_value': sum(h['value'] for h in holdings),
        'positions': len(holdings),
        'holdings': holdings,
    }
