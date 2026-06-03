"""
Headless tests for the data-layer logic behind Stock Monitor.

Verifies, WITHOUT opening the GUI:
  1. Watchlist persistence (save -> load round-trip)
  2. EMA computation (5/10/20) against a hand-checked value
  3. QuoteFetcher  -> latest price + % change   (needs internet)
  4. SearchWorker  -> autocomplete suggestions   (needs internet)

The QThread workers are run synchronously by calling .run() directly and
capturing the signals they emit (same-thread = direct connection, so no Qt
event loop is required).

Run:
    python test_functions.py
"""

import os
import sys
import json
import tempfile

import pandas as pd
from PyQt6.QtWidgets import QApplication

import main  # the app module (must be importable from same folder)


# ── tiny assertion helpers ──────────────────────────────────────────────────
PASS, FAIL = 0, 0

def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {name}' + (f'  ({detail})' if detail else ''))
    else:
        FAIL += 1
        print(f'  [FAIL] {name}' + (f'  ({detail})' if detail else ''))


# ── 1. watchlist persistence ────────────────────────────────────────────────
def test_persistence():
    print('\n[1] Watchlist persistence')
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    orig = main.WATCHLIST_FILE
    try:
        main.WATCHLIST_FILE = path
        win = main.MainWindow.__new__(main.MainWindow)  # no GUI init
        win._watchlist = ['AAPL', 'MSFT', 'GOOG']
        win._save_watchlist()

        with open(path) as f:
            on_disk = json.load(f)
        check('saved file matches in-memory list', on_disk == ['AAPL', 'MSFT', 'GOOG'],
              detail=str(on_disk))

        reloaded = win._load_watchlist()
        check('reload round-trips correctly', reloaded == ['AAPL', 'MSFT', 'GOOG'],
              detail=str(reloaded))
    finally:
        main.WATCHLIST_FILE = orig
        os.remove(path)


# ── 2. EMA computation ──────────────────────────────────────────────────────
def test_ema():
    print('\n[2] EMA computation')
    # constant series -> EMA equals the constant
    const = pd.Series([10.0] * 30)
    ema = const.ewm(span=5, adjust=False).mean()
    check('EMA of constant series == constant', abs(ema.iloc[-1] - 10.0) < 1e-9,
          detail=f'{ema.iloc[-1]:.4f}')

    # known recursive value: EMA_t = price*k + EMA_{t-1}*(1-k), k = 2/(span+1)
    prices = pd.Series([1, 2, 3, 4, 5], dtype=float)
    k = 2 / (3 + 1)              # span = 3 -> k = 0.5
    manual = prices.iloc[0]
    for p in prices.iloc[1:]:
        manual = p * k + manual * (1 - k)
    pandas_ema = prices.ewm(span=3, adjust=False).mean().iloc[-1]
    check('span=3 EMA matches manual recursion',
          abs(manual - pandas_ema) < 1e-9, detail=f'{pandas_ema:.4f} vs {manual:.4f}')

    # all three spans the app draws should produce finite, ordered-length output
    for span in main.EMAS:
        out = prices.ewm(span=span, adjust=False).mean()
        check(f'{span} EMA returns {len(prices)} finite values',
              len(out) == len(prices) and out.notna().all())


# ── helper to run a QThread worker synchronously ────────────────────────────
def run_worker(worker, signal_name):
    captured = []
    getattr(worker, signal_name).connect(lambda *a: captured.append(a))
    worker.run()            # synchronous, same thread
    return captured


# ── 3. QuoteFetcher (network) ───────────────────────────────────────────────
def test_quotes():
    print('\n[3] QuoteFetcher  (requires internet)')
    try:
        results = run_worker(main.QuoteFetcher(['AAPL', 'MSFT']), 'quote_ready')
    except Exception as exc:
        check('QuoteFetcher ran without exception', False, detail=str(exc))
        return

    check('received at least one quote', len(results) >= 1, detail=f'{len(results)} quotes')
    for sym, price, pct in results:
        check(f'{sym}: price > 0', price > 0, detail=f'{price:.2f}')
        check(f'{sym}: pct change is a sane number', -90 < pct < 90, detail=f'{pct:+.2f}%')


# ── 4. SearchWorker / autocomplete (network) ────────────────────────────────
def test_search():
    print('\n[4] SearchWorker autocomplete  (requires internet)')
    import time as _time
    suggestions = []
    # Yahoo throttles bursts: retry a few times before declaring failure.
    for attempt in range(4):
        try:
            results = run_worker(main.SearchWorker('apple'), 'results_ready')
        except Exception as exc:
            check('SearchWorker ran without exception', False, detail=str(exc))
            return
        suggestions = results[0][0] if results else []
        if suggestions:
            break
        _time.sleep(2)

    check('search returned suggestions (after retries)', len(suggestions) >= 1,
          detail=f'{len(suggestions)} hits')
    if not suggestions:
        return
    check('got >=1 suggestion for "apple"', len(suggestions) >= 1,
          detail=f'{len(suggestions)} hits')
    symbols = [sym for _, sym in suggestions]
    check('AAPL appears in suggestions for "apple"', 'AAPL' in symbols,
          detail=str(symbols[:5]))
    check('each suggestion is (display, symbol)',
          all(isinstance(d, str) and isinstance(s, str) for d, s in suggestions))


# ── 5. portfolio P&L calculation ────────────────────────────────────────────
def test_portfolio():
    print('\n[5] Portfolio P&L calculation')
    holdings = [
        {'symbol': 'AAA', 'shares': 10, 'avg_cost': 100.0},   # cost 1000
        {'symbol': 'BBB', 'shares': 5,  'avg_cost': 200.0},   # cost 1000
    ]
    prices = {'AAA': 110.0, 'BBB': 180.0}                     # mkt 1100 / 900
    res = main.compute_portfolio(holdings, prices, cash=500.0)

    check('invested cost == 2000', abs(res['invested_cost'] - 2000) < 1e-9,
          detail=f"{res['invested_cost']:.2f}")
    check('market value == 2000', abs(res['market_value'] - 2000) < 1e-9,
          detail=f"{res['market_value']:.2f}")
    check('total value == 2500 (incl. cash)', abs(res['total_value'] - 2500) < 1e-9,
          detail=f"{res['total_value']:.2f}")
    check('total P&L == 0', abs(res['total_pnl']) < 1e-9, detail=f"{res['total_pnl']:.2f}")

    aaa = res['rows'][0]
    check('AAA P&L == +100', abs(aaa['pnl'] - 100) < 1e-9, detail=f"{aaa['pnl']:.2f}")
    check('AAA P&L%% == +10%%', abs(aaa['pnl_pct'] - 10) < 1e-9, detail=f"{aaa['pnl_pct']:.2f}%")

    # a missing price must not corrupt the totals
    res2 = main.compute_portfolio(holdings, {'AAA': 110.0}, cash=0.0)
    check('missing price -> row price is None', res2['rows'][1]['price'] is None)
    check('missing price excluded from market value', abs(res2['market_value'] - 1100) < 1e-9,
          detail=f"{res2['market_value']:.2f}")


# ── 6. portfolio CSV import ──────────────────────────────────────────────────
def test_csv_parse():
    print('\n[6] Portfolio CSV import')
    content = 'symbol,shares,avg_cost\nAAPL,10,150.5\nMSFT,5,300\nCASH,1000,\n'
    fd, path = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    with open(path, 'w', newline='') as f:
        f.write(content)
    try:
        holdings, cash = main.parse_portfolio_csv(path)
        check('parsed 2 holdings (header + CASH skipped)', len(holdings) == 2,
              detail=str(len(holdings)))
        check('cash == 1000', abs(cash - 1000) < 1e-9, detail=str(cash))
        check('AAPL row parsed correctly',
              holdings[0] == {'symbol': 'AAPL', 'shares': 10.0, 'avg_cost': 150.5},
              detail=str(holdings[0]))
    finally:
        os.remove(path)


# ── 7. LLM prompt builder (pure, offline) ───────────────────────────────────
def test_llm_prompt():
    print('\n[7] LLM prompt builder (offline)')
    import llm
    holdings = [
        {'symbol': 'AAA', 'shares': 10, 'avg_cost': 100.0},
        {'symbol': 'BBB', 'shares': 5,  'avg_cost': 200.0},
    ]
    result = main.compute_portfolio(holdings, {'AAA': 110.0, 'BBB': 180.0}, cash=500.0)
    watchlist = {'CCC': 42.0, 'DDD': None}

    context = llm.build_portfolio_context(result, watchlist)
    check('context includes a held symbol', 'AAA' in context)
    check('context includes totals', 'total_value' in context)
    check('context includes watchlist symbol', 'CCC' in context)
    check('context marks missing watchlist price as n/a', 'DDD: n/a' in context, detail='')

    prompt = llm.build_user_prompt(result, watchlist, 'What are my risks?')
    check('prompt embeds the question', 'What are my risks?' in prompt)
    check('prompt embeds the data block', 'Current holdings' in prompt)

    # empty question falls back to a default ask
    prompt2 = llm.build_user_prompt(result, watchlist, '')
    check('empty question gets a default', 'analysis' in prompt2.lower())

    # system prompt is non-empty and frozen (for prompt caching)
    check('system prompt is substantial', len(llm.SYSTEM_PROMPT) > 200)
    check('model is opus-4-8', llm.MODEL == 'claude-opus-4-8', detail=llm.MODEL)


# ── 8. data cache layer (offline) ────────────────────────────────────────────
def test_cache():
    print('\n[8] Data cache layer (offline)')
    import data as datamod

    datamod.clear_cache()
    calls = {'n': 0}
    fake_df = pd.DataFrame({'Close': [1.0, 2.0, 3.0]})

    def fake_fetcher(sym, period, interval):
        calls['n'] += 1
        return fake_df

    clock = {'t': 1000.0}
    now = lambda: clock['t']

    # first call hits the fetcher
    datamod.get_history('AAA', '1mo', '1d', ttl=300, fetcher=fake_fetcher, now=now)
    check('first call fetches', calls['n'] == 1, detail=f"calls={calls['n']}")

    # second call within TTL is served from cache (no new fetch)
    datamod.get_history('AAA', '1mo', '1d', ttl=300, fetcher=fake_fetcher, now=now)
    check('second call within TTL is cached', calls['n'] == 1, detail=f"calls={calls['n']}")

    # advancing past the TTL triggers a refetch
    clock['t'] += 301
    datamod.get_history('AAA', '1mo', '1d', ttl=300, fetcher=fake_fetcher, now=now)
    check('refetch after TTL expiry', calls['n'] == 2, detail=f"calls={calls['n']}")

    # retry: a fetcher that always fails, with stale cache present -> serves stale
    def failing(sym, period, interval):
        raise RuntimeError('simulated Yahoo outage')

    clock['t'] += 301
    df = datamod.get_history('AAA', '1mo', '1d', ttl=300, fetcher=failing, now=now)
    check('serves stale data when source fails', df is fake_df)

    datamod.clear_cache()


# ── 9. price-alert evaluation (offline) ──────────────────────────────────────
def test_alerts():
    print('\n[9] Price-alert evaluation (offline)')
    alerts = [
        {'symbol': 'AAA', 'condition': 'above', 'price': 100.0, 'enabled': True,  'triggered': False},
        {'symbol': 'AAA', 'condition': 'below', 'price': 50.0,  'enabled': True,  'triggered': False},
        {'symbol': 'BBB', 'condition': 'above', 'price': 10.0,  'enabled': False, 'triggered': False},
    ]

    # price 105 -> only the 'above 100' alert fires
    fired = main.evaluate_alerts(alerts, 'AAA', 105.0)
    check('above-threshold fires once', len(fired) == 1 and fired[0]['price'] == 100.0,
          detail=str([a['price'] for a in fired]))
    check('fired alert is marked triggered', alerts[0]['triggered'] is True)

    # firing again does NOT re-fire (already triggered)
    fired2 = main.evaluate_alerts(alerts, 'AAA', 106.0)
    check('does not re-fire until re-armed', fired2 == [])

    # the 'below 50' alert fires when price drops
    fired3 = main.evaluate_alerts(alerts, 'AAA', 40.0)
    check('below-threshold fires', len(fired3) == 1 and fired3[0]['condition'] == 'below')

    # disabled alert never fires
    fired4 = main.evaluate_alerts(alerts, 'BBB', 999.0)
    check('disabled alert is ignored', fired4 == [])

    # wrong symbol ignored
    check('other symbols ignored', main.evaluate_alerts(alerts, 'ZZZ', 1e9) == [])


# ── 10. technical indicators (offline) ───────────────────────────────────────
def test_indicators():
    print('\n[10] Technical indicators (offline)')
    import indicators as ta

    # RSI of a strictly rising series approaches 100
    rising = pd.Series([float(i) for i in range(1, 40)])
    r = ta.rsi(rising, n=14)
    check('RSI of monotonic-up series ~100', r.iloc[-1] > 99, detail=f'{r.iloc[-1]:.2f}')

    # RSI of a strictly falling series approaches 0
    falling = pd.Series([float(i) for i in range(40, 1, -1)])
    rf = ta.rsi(falling, n=14)
    check('RSI of monotonic-down series ~0', rf.iloc[-1] < 1, detail=f'{rf.iloc[-1]:.2f}')

    # Bollinger: price sits between bands; mid == SMA
    close = pd.Series([10, 11, 12, 11, 10, 12, 13, 14, 13, 12,
                       11, 12, 13, 14, 15, 16, 15, 14, 13, 14], dtype=float)
    mid, up, lo = ta.bollinger(close, n=20, k=2)
    check('Bollinger mid == 20-SMA', abs(mid.iloc[-1] - close.mean()) < 1e-9)
    check('upper band above mid', up.iloc[-1] > mid.iloc[-1])
    check('lower band below mid', lo.iloc[-1] < mid.iloc[-1])

    # MACD: histogram == line - signal
    line, sig, hist = ta.macd(close)
    check('MACD hist == line - signal', abs((hist - (line - sig)).abs().max()) < 1e-9)
    # for a rising tail, MACD line should be positive
    check('MACD line positive on uptrend', line.iloc[-1] > 0, detail=f'{line.iloc[-1]:.3f}')


# ── 11. sentiment parsing + classification (offline) ─────────────────────────
def test_sentiment():
    print('\n[11] Sentiment parsing & classification (offline)')
    import sentiment as sent

    # classify thresholds
    checks = [(5, 'Extreme Fear'), (35, 'Fear'), (50, 'Neutral'),
              (65, 'Greed'), (90, 'Extreme Greed')]
    for score, expected in checks:
        label, color = sent.classify(score)
        check(f'classify({score}) == {expected}', label == expected, detail=label)
    check('classify(None) is safe', sent.classify(None)[0] == 'Unknown')

    # parse a CNN-shaped payload
    cnn_payload = {
        'fear_and_greed': {'score': 57.0, 'rating': 'greed', 'timestamp': 't'},
        'market_momentum_sp500': {'score': 97.6, 'rating': 'extreme greed'},
        'put_call_options': {'score': 98.4, 'rating': 'extreme greed'},
        'junk_bond_demand': {'score': 0.4, 'rating': 'extreme fear'},
        'fear_and_greed_historical': {'should': 'be ignored'},
    }
    parsed = sent.parse_cnn(cnn_payload)
    check('CNN score parsed', abs(parsed['score'] - 57.0) < 1e-9)
    check('CNN components extracted', len(parsed['components']) == 3,
          detail=str(len(parsed['components'])))
    check('CNN component has label+score',
          parsed['components'][0]['label'] and parsed['components'][0]['score'] is not None)

    # parse crypto payload
    crypto = sent.parse_crypto({'data': [{'value': '11', 'value_classification': 'Extreme Fear'}]})
    check('crypto score parsed', crypto['score'] == 11.0 and crypto['rating'] == 'Extreme Fear')

    # VIX mood
    check('low VIX = Calm', sent.vix_mood(12)[0] == 'Calm')
    check('high VIX = High Fear', sent.vix_mood(35)[0] == 'High Fear')


def main_run():
    QApplication(sys.argv)   # needed so QObject/QThread can be constructed
    print('=' * 60)
    print('Stock Monitor — function tests')
    print('=' * 60)

    test_persistence()
    test_ema()
    test_portfolio()
    test_csv_parse()
    test_llm_prompt()
    test_cache()
    test_alerts()
    test_indicators()
    test_sentiment()
    test_quotes()
    test_search()

    print('\n' + '=' * 60)
    print(f'RESULTS:  {PASS} passed, {FAIL} failed')
    print('=' * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main_run()
