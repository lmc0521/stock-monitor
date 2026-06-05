"""Background QThread workers (network off the UI thread)."""

import json
from urllib.request import Request, urlopen
from urllib.parse import quote_plus

from PyQt6.QtCore import QThread, pyqtSignal

import data
import llm
import sentiment
import history
import thirteenf
import ipo
import analysis


class AnalysisWorker(QThread):
    """Fetches analyst targets + technical levels for one symbol."""
    ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol

    def run(self):
        try:
            self.ready.emit(analysis.fetch_analysis(self.symbol))
        except Exception as exc:
            self.error.emit(str(exc))


class IPOWorker(QThread):
    """Fetches the Nasdaq IPO calendar for a month off the UI thread."""
    ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, month: str):
        super().__init__()
        self.month = month

    def run(self):
        try:
            self.ready.emit(ipo.fetch_calendar(self.month))
        except Exception as exc:
            self.error.emit(str(exc))


class RumoredIPOWorker(QThread):
    """Streams an AI summary of rumored/expected IPOs from recent news."""
    chunk = pyqtSignal(str)
    done  = pyqtSignal()
    error = pyqtSignal(str)

    def run(self):
        try:
            for text in llm.stream_rumored_ipos():
                self.chunk.emit(text)
            self.done.emit()
        except Exception as exc:
            self.error.emit(str(exc))


class DataFetcher(QThread):
    """Fetches OHLCV history for the chart."""
    data_ready = pyqtSignal(object, str)
    error      = pyqtSignal(str)

    def __init__(self, symbol: str, period: str, interval: str):
        super().__init__()
        self.symbol, self.period, self.interval = symbol, period, interval

    def run(self):
        try:
            df = data.get_history(self.symbol, self.period, self.interval)
            if df is None or df.empty:
                self.error.emit(f'No data returned for "{self.symbol}"')
            else:
                self.data_ready.emit(df, self.symbol)
        except Exception as exc:
            self.error.emit(str(exc))


class QuoteFetcher(QThread):
    """Fetches latest close + % change for every symbol in the watchlist."""
    quote_ready = pyqtSignal(str, float, float)   # symbol, last_price, pct_change

    def __init__(self, symbols: list):
        super().__init__()
        self.symbols = symbols

    def run(self):
        for sym in self.symbols:
            try:
                closes = data.get_last_closes(sym, days=5)
                if len(closes) >= 2:
                    last, prev = closes.iloc[-1], closes.iloc[-2]
                    pct = (last - prev) / prev * 100.0
                    self.quote_ready.emit(sym, float(last), float(pct))
                elif len(closes) == 1:
                    self.quote_ready.emit(sym, float(closes.iloc[-1]), 0.0)
            except Exception:
                continue


class SearchWorker(QThread):
    """Queries Yahoo Finance's symbol-search endpoint for autocomplete."""
    results_ready = pyqtSignal(list)   # list[(display, symbol)]

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self):
        try:
            url = (
                'https://query2.finance.yahoo.com/v1/finance/search'
                f'?q={quote_plus(self.query)}&quotesCount=8&newsCount=0'
            )
            req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urlopen(req, timeout=5) as resp:
                data = json.load(resp)
            out = []
            for q in data.get('quotes', []):
                sym = q.get('symbol')
                if not sym:
                    continue
                name = q.get('shortname') or q.get('longname') or ''
                exch = q.get('exchDisp') or ''
                label = f'{sym} — {name}' + (f'  ({exch})' if exch else '')
                out.append((label, sym))
            self.results_ready.emit(out)
        except Exception:
            self.results_ready.emit([])


class PriceFetcher(QThread):
    """Fetches the latest close price for a set of symbols (for the portfolio)."""
    prices_ready = pyqtSignal(dict)   # {symbol: price}

    def __init__(self, symbols: list):
        super().__init__()
        self.symbols = symbols

    def run(self):
        out = {}
        for sym in self.symbols:
            try:
                closes = data.get_last_closes(sym, days=5)
                if len(closes):
                    out[sym] = float(closes.iloc[-1])
            except Exception:
                continue
        self.prices_ready.emit(out)


class SentimentWorker(QThread):
    """Fetches investor-sentiment indices off the UI thread."""
    ready = pyqtSignal(dict)

    def run(self):
        self.ready.emit(sentiment.gather())


class HistoryWorker(QThread):
    """Builds the portfolio value-history series off the UI thread."""
    ready = pyqtSignal(object, object)   # total Series, invested Series
    error = pyqtSignal(str)

    def __init__(self, holdings: list, cash: float):
        super().__init__()
        self.holdings = holdings
        self.cash = cash

    def run(self):
        try:
            total, invested = history.build_history(self.holdings, self.cash)
            self.ready.emit(total, invested)
        except Exception as exc:
            self.error.emit(str(exc))


class ThirteenFWorker(QThread):
    """Fetches a manager's latest 13F holdings from SEC EDGAR off the UI thread."""
    ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, cik: str):
        super().__init__()
        self.cik = cik

    def run(self):
        try:
            self.ready.emit(thirteenf.get_holdings(self.cik))
        except Exception as exc:
            self.error.emit(str(exc))


class InsightsWorker(QThread):
    """Streams a portfolio-aware Claude analysis, emitting text as it arrives."""
    chunk = pyqtSignal(str)
    done  = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, question: str, portfolio_result: dict, watchlist_quotes: dict):
        super().__init__()
        self.question = question
        self.portfolio_result = portfolio_result
        self.watchlist_quotes = watchlist_quotes

    def run(self):
        try:
            for text in llm.stream_insights(
                self.question, self.portfolio_result, self.watchlist_quotes
            ):
                self.chunk.emit(text)
            self.done.emit()
        except Exception as exc:
            self.error.emit(str(exc))


class StrategyWorker(QThread):
    """Streams a firm's market-outlook summary from Claude (web search)."""
    chunk = pyqtSignal(str)
    done  = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, firm: str):
        super().__init__()
        self.firm = firm

    def run(self):
        try:
            for text in llm.stream_firm_strategy(self.firm):
                self.chunk.emit(text)
            self.done.emit()
        except Exception as exc:
            self.error.emit(str(exc))


# ── list row widget ─────────────────────────────────────────────────────────
