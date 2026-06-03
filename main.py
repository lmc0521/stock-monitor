import sys
import json
import os
import csv
from urllib.request import Request, urlopen
from urllib.parse import quote_plus

import matplotlib
matplotlib.use('QtAgg')
from matplotlib.lines import Line2D

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QListWidgetItem, QLineEdit, QLabel,
    QMessageBox, QSizePolicy, QFrame, QCompleter, QDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QFileDialog, QTextEdit,
    QFormLayout, QInputDialog, QComboBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QStringListModel, QTimer
from PyQt6.QtGui import QFont, QColor

import mplfinance as mpf
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

import llm
import data
import indicators as ta

def _app_dir() -> str:
    """Directory for read/write state — next to the .exe when frozen, else the source dir."""
    if getattr(sys, 'frozen', False):           # running as a PyInstaller bundle
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


_APP_DIR = _app_dir()
WATCHLIST_FILE = os.path.join(_APP_DIR, 'watchlist.json')
PORTFOLIO_FILE = os.path.join(_APP_DIR, 'portfolio.json')
ALERTS_FILE    = os.path.join(_APP_DIR, 'alerts.json')

PERIODS = {
    '1D':  ('1d',  '5m'),
    '5D':  ('5d',  '15m'),
    '1M':  ('1mo', '1d'),
    '3M':  ('3mo', '1d'),
    '6M':  ('6mo', '1d'),
    '1Y':  ('1y',  '1d'),
    '2Y':  ('2y',  '1wk'),
    '5Y':  ('5y',  '1wk'),
}

# EMA spans -> line colour
EMAS = {
    5:  '#f5d142',   # yellow
    10: '#42c5f5',   # cyan
    20: '#f542e6',   # magenta
}

DARK_BG   = '#1a1a2e'
PANEL_BG  = '#16213e'
ACCENT    = '#0f3460'
HIGHLIGHT = '#e94560'
TEXT      = '#e0e0e0'
SUBTEXT   = '#888'
UP_COLOR  = '#26a69a'
DOWN_COLOR= '#ef5350'


# ── portfolio logic (pure, unit-testable) ─────────────────────────────────────

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
            holdings.append({'symbol': sym.upper(), 'shares': shares, 'avg_cost': avg_cost})
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


# ── list row widget ─────────────────────────────────────────────────────────

class StockRow(QWidget):
    """A watchlist row: symbol on the left, price + coloured % on the right."""
    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        self._sym = QLabel(symbol)
        self._sym.setFont(QFont('Segoe UI', 11, QFont.Weight.Bold))
        self._sym.setStyleSheet('background: transparent;')

        self._price = QLabel('…')
        self._price.setFont(QFont('Segoe UI', 10))
        self._price.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._price.setStyleSheet(f'color: {SUBTEXT}; background: transparent;')

        layout.addWidget(self._sym)
        layout.addStretch()
        layout.addWidget(self._price)

    def set_quote(self, price: float, pct: float):
        color = UP_COLOR if pct >= 0 else DOWN_COLOR
        arrow = '▲' if pct >= 0 else '▼'
        self._price.setText(f'{price:,.2f}  {arrow} {abs(pct):.2f}%')
        self._price.setStyleSheet(f'color: {color}; background: transparent;')

    def set_alarm(self, on: bool):
        """Highlight the row when one of its price alerts has fired."""
        self._sym.setText(('🔔 ' if on else '') + self.symbol)
        self.setStyleSheet('background:#3a1620; border-radius:4px;' if on else '')


# ── chart panel ────────────────────────────────────────────────────────────

class ChartPanel(QWidget):
    LONG_PRESS_MS = 200          # how long to hold before the crosshair appears

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

        self._placeholder = QLabel('Select a stock from the watchlist')
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setFont(QFont('Segoe UI', 14))
        self._placeholder.setStyleSheet(f'color: {SUBTEXT};')
        self._layout.addWidget(self._placeholder)

        self._canvas = None

        # crosshair state
        self._df = None
        self._ax = None
        self._intraday = False
        self._vline = self._hline = self._annot = None
        self._active = False
        self._last_event = None

        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.setInterval(self.LONG_PRESS_MS)
        self._press_timer.timeout.connect(self._activate)

    def render(self, df, symbol: str, period_key: str, indicators=None):
        if self._canvas:
            self._layout.removeWidget(self._canvas)
            self._canvas.deleteLater()
            self._canvas = None
        self._placeholder.hide()
        self._active = False
        self._press_timer.stop()
        indicators = indicators or set()

        style = mpf.make_mpf_style(
            base_mpf_style='nightclouds',
            gridstyle=':', gridcolor='#2a2a4a',
            facecolor='#0d0d1a', figcolor=DARK_BG,
            rc={'axes.labelcolor': TEXT, 'xtick.color': SUBTEXT, 'ytick.color': SUBTEXT},
        )

        close = df['Close']
        n = len(df)

        # panel layout: 0 = price, 1 = volume, then one panel per oscillator
        next_panel = 2
        rsi_panel = macd_panel = None
        panel_ratios = [3, 1]
        if 'RSI' in indicators:
            rsi_panel = next_panel; next_panel += 1; panel_ratios.append(1.4)
        if 'MACD' in indicators:
            macd_panel = next_panel; next_panel += 1; panel_ratios.append(1.4)

        # EMA overlays (price panel)
        addplots = []
        for span, color in EMAS.items():
            addplots.append(mpf.make_addplot(
                close.ewm(span=span, adjust=False).mean(), color=color, width=1.0))

        # Bollinger Bands (price panel)
        if 'BBANDS' in indicators:
            mid, up, lo = ta.bollinger(close)
            addplots += [
                mpf.make_addplot(up,  color='#7e8aa2', width=0.8),
                mpf.make_addplot(mid, color='#7e8aa2', width=0.6, linestyle='--'),
                mpf.make_addplot(lo,  color='#7e8aa2', width=0.8),
            ]

        # RSI panel (with 70/30 guide lines)
        if rsi_panel is not None:
            addplots += [
                mpf.make_addplot(ta.rsi(close), panel=rsi_panel, color='#f5d142',
                                 width=1.0, ylabel='RSI'),
                mpf.make_addplot([70] * n, panel=rsi_panel, color=DOWN_COLOR, width=0.6),
                mpf.make_addplot([30] * n, panel=rsi_panel, color=UP_COLOR,   width=0.6),
            ]

        # MACD panel (histogram + line + signal)
        if macd_panel is not None:
            line, sig, hist = ta.macd(close)
            bar_colors = [UP_COLOR if h >= 0 else DOWN_COLOR for h in hist.fillna(0)]
            addplots += [
                mpf.make_addplot(hist, panel=macd_panel, type='bar',
                                 color=bar_colors, width=0.7, alpha=0.5),
                mpf.make_addplot(line, panel=macd_panel, color='#42c5f5',
                                 width=1.0, ylabel='MACD'),
                mpf.make_addplot(sig,  panel=macd_panel, color='#f542e6', width=1.0),
            ]

        fig, axlist = mpf.plot(
            df, type='candle', style=style, volume=True,
            addplot=addplots, panel_ratios=tuple(panel_ratios),
            returnfig=True, figsize=(12, 8),
            title=f'\n{symbol}  [{period_key}]',
        )
        fig.patch.set_facecolor(DARK_BG)

        # manual legend for the EMA lines
        handles = [Line2D([0], [0], color=c, lw=1.5, label=f'{s} EMA')
                   for s, c in EMAS.items()]
        leg = axlist[0].legend(handles=handles, loc='upper left',
                               framealpha=0.3, fontsize=9)
        for txt in leg.get_texts():
            txt.set_color(TEXT)

        # ── crosshair setup ──
        self._df = df
        self._ax = axlist[0]                 # main price panel
        self._intraday = df.index.to_series().dt.time.nunique() > 1

        # lock the axis limits BEFORE adding crosshair lines, otherwise the
        # initial axhline/axvline positions get pulled into the autoscale.
        xlim, ylim = self._ax.get_xlim(), self._ax.get_ylim()
        self._vline = self._ax.axvline(xlim[0], color='#aaa', lw=0.8, ls='--', visible=False, zorder=8)
        self._hline = self._ax.axhline(ylim[0], color='#aaa', lw=0.8, ls='--', visible=False, zorder=8)
        self._ax.set_xlim(xlim)
        self._ax.set_ylim(ylim)
        self._ax.set_autoscale_on(False)
        self._annot = self._ax.annotate(
            '', xy=(0, 0), xytext=(15, 15), textcoords='offset points',
            ha='left', va='bottom', fontsize=9, color=TEXT, zorder=10, visible=False,
            bbox=dict(boxstyle='round,pad=0.5', fc=PANEL_BG, ec=HIGHLIGHT, lw=1.0),
        )

        self._canvas = FigureCanvas(fig)
        self._canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._canvas.mpl_connect('button_press_event', self._on_press)
        self._canvas.mpl_connect('button_release_event', self._on_release)
        self._canvas.mpl_connect('motion_notify_event', self._on_move)
        self._layout.addWidget(self._canvas)
        self._canvas.draw()

    # ── crosshair handlers ──
    def _on_press(self, event):
        if event.inaxes != self._ax or self._df is None:
            return
        self._last_event = event
        self._press_timer.start()      # wait for a long-press before activating

    def _activate(self):
        if self._last_event is not None:
            self._active = True
            self._update_cursor(self._last_event)

    def _on_move(self, event):
        if event.inaxes != self._ax:
            return
        self._last_event = event
        if self._active:
            self._update_cursor(event)

    def _on_release(self, event):
        self._press_timer.stop()
        if self._active:
            self._active = False
            for artist in (self._vline, self._hline, self._annot):
                artist.set_visible(False)
            self._canvas.draw_idle()

    def _update_cursor(self, event):
        if event.xdata is None or event.ydata is None:
            return
        idx = max(0, min(int(round(event.xdata)), len(self._df) - 1))
        row = self._df.iloc[idx]
        date = self._df.index[idx]
        fmt = '%Y-%m-%d  %H:%M' if self._intraday else '%Y-%m-%d'

        self._vline.set_xdata([idx, idx])
        self._vline.set_visible(True)
        self._hline.set_ydata([event.ydata, event.ydata])
        self._hline.set_visible(True)

        self._annot.xy = (idx, event.ydata)
        self._annot.set_text(
            f"{date.strftime(fmt)}\n"
            f"O {row['Open']:.2f}   H {row['High']:.2f}\n"
            f"L {row['Low']:.2f}   C {row['Close']:.2f}\n"
            f"Cursor  {event.ydata:.2f}"
        )
        # flip the label to the left near the right edge so it stays on-screen
        if idx > len(self._df) * 0.75:
            self._annot.set_ha('right')
            self._annot.set_position((-15, 15))
        else:
            self._annot.set_ha('left')
            self._annot.set_position((15, 15))
        self._annot.set_visible(True)
        self._canvas.draw_idle()


# ── portfolio dialog ──────────────────────────────────────────────────────────

class AddHoldingDialog(QDialog):
    """Add a single holding, with company-name autocomplete and symbol validation."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Add holding')
        self.setStyleSheet(parent.styleSheet() if parent else '')
        self.result_holding = None
        self._sugg_map = {}
        self._search_job = None
        self._validate_job = None
        self._pending = None

        form = QFormLayout(self)
        self._sym    = QLineEdit(); self._sym.setPlaceholderText('Company name or symbol…')
        self._sym.textEdited.connect(lambda _t: self._search_timer.start())
        self._shares = QLineEdit(); self._shares.setPlaceholderText('e.g. 10')
        self._cost   = QLineEdit(); self._cost.setPlaceholderText('e.g. 180.50')
        form.addRow('Symbol', self._sym)
        form.addRow('Shares', self._shares)
        form.addRow('Avg cost', self._cost)

        # autocomplete (same Yahoo search as the watchlist)
        self._completer = QCompleter(self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer_model = QStringListModel(self)
        self._completer.setModel(self._completer_model)
        self._completer.activated[str].connect(self._on_suggestion_picked)
        self._sym.setCompleter(self._completer)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._run_search)

        self._status = QLabel('')
        self._status.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        form.addRow(self._status)

        btns = QHBoxLayout()
        self._ok = QPushButton('Add')
        self._ok.setObjectName('AccentBtn')
        self._ok.clicked.connect(self._accept)
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        btns.addWidget(self._ok); btns.addWidget(cancel)
        form.addRow(btns)

    # ── autocomplete ──
    def _run_search(self):
        query = self._sym.text().strip()
        if len(query) < 2:
            return
        if self._search_job and self._search_job.isRunning():
            self._search_job.terminate(); self._search_job.wait()
        self._search_job = SearchWorker(query)
        self._search_job.results_ready.connect(self._on_search_results)
        self._search_job.start()

    def _on_search_results(self, results: list):
        self._sugg_map = {disp: sym for disp, sym in results}
        self._completer_model.setStringList(list(self._sugg_map))
        if results:
            self._completer.complete()

    def _on_suggestion_picked(self, display: str):
        sym = self._sugg_map.get(display)
        if sym:
            QTimer.singleShot(0, lambda: self._sym.setText(sym))

    # ── add + validate ──
    def _accept(self):
        text = self._sym.text().strip()
        sym = self._sugg_map.get(text, text).upper()
        if not sym:
            QMessageBox.warning(self, 'Invalid', 'Symbol is required.')
            return
        try:
            shares = float(self._shares.text())
            cost   = float(self._cost.text())
        except ValueError:
            QMessageBox.warning(self, 'Invalid', 'Shares and avg cost must be numbers.')
            return

        # validate the symbol actually has market data before accepting
        self._pending = {'symbol': sym, 'shares': shares, 'avg_cost': cost}
        self._ok.setEnabled(False)
        self._status.setText(f'Checking {sym} …')
        self._validate_job = PriceFetcher([sym])
        self._validate_job.prices_ready.connect(self._on_validated)
        self._validate_job.start()

    def _on_validated(self, prices: dict):
        sym = self._pending['symbol']
        self._ok.setEnabled(True)
        if sym in prices:
            self.result_holding = self._pending
            self.accept()
        else:
            self._status.setText('')
            QMessageBox.warning(
                self, 'Symbol not found',
                f"Couldn't find market data for \"{sym}\".\n\n"
                "Check the spelling, or start typing the company name and pick "
                "a suggestion from the list.")


class PortfolioDialog(QDialog):
    HEADERS = ['Symbol', 'Shares', 'Avg Cost', 'Price',
               'Mkt Value', 'Cost', 'P&L', 'P&L %']

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Portfolio / P&L')
        self.resize(1140, 520)

        self._holdings, self._cash = load_portfolio()
        self._prices = {}
        self._fetcher = None

        self._build_ui()
        if self._holdings:
            self._refresh_prices()
        else:
            self._status.setText('No portfolio loaded — click "Import CSV…" '
                                 '(columns: symbol, shares, avg_cost; use CASH for cash).')

    def _build_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        add = QPushButton('+ Add holding')
        add.setObjectName('AccentBtn')
        add.clicked.connect(self._add_holding)
        rm = QPushButton('Remove holding')
        rm.clicked.connect(self._remove_holding)
        cash_btn = QPushButton('Set cash…')
        cash_btn.clicked.connect(self._set_cash)
        imp = QPushButton('Import CSV…')
        imp.clicked.connect(self._import)
        ref = QPushButton('↻ Refresh prices')
        ref.clicked.connect(self._refresh_prices)
        top.addWidget(add)
        top.addWidget(rm)
        top.addWidget(cash_btn)
        top.addWidget(imp)
        top.addWidget(ref)
        top.addStretch()
        layout.addLayout(top)

        mid = QHBoxLayout()
        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        mid.addWidget(self._table, 3)

        pie_container = QWidget()
        pie_container.setFixedWidth(300)
        self._pie_layout = QVBoxLayout(pie_container)
        self._pie_layout.setContentsMargins(0, 0, 0, 0)
        self._pie_canvas = None
        mid.addWidget(pie_container, 0)
        layout.addLayout(mid)

        self._summary = QLabel('')
        self._summary.setFont(QFont('Segoe UI', 11, QFont.Weight.Bold))
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._status = QLabel('')
        self._status.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        layout.addWidget(self._status)

    # ── actions ──
    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Import portfolio CSV', '', 'CSV files (*.csv);;All files (*)')
        if not path:
            return
        try:
            holdings, cash = parse_portfolio_csv(path)
        except Exception as exc:
            QMessageBox.warning(self, 'Import error', str(exc))
            return
        if not holdings and cash == 0:
            QMessageBox.warning(self, 'Import error', 'No valid rows found in that CSV.')
            return
        self._holdings, self._cash = holdings, cash
        save_portfolio(holdings, cash)
        self._status.setText(f'Imported {len(holdings)} holdings + cash {cash:,.2f}.')
        self._refresh_prices()

    def _add_holding(self):
        dlg = AddHoldingDialog(self)
        if dlg.exec() and dlg.result_holding:
            h = dlg.result_holding
            existing = next((x for x in self._holdings if x['symbol'] == h['symbol']), None)
            if existing:
                existing.update(h)              # overwrite shares/cost for that symbol
            else:
                self._holdings.append(h)
            save_portfolio(self._holdings, self._cash)
            self._status.setText(f"Saved {h['symbol']}.")
            self._refresh_prices()

    def _remove_holding(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._status.setText('Select a holding row first, then click Remove.')
            return
        i = rows[0].row()
        if not (0 <= i < len(self._holdings)):
            return
        sym = self._holdings[i]['symbol']
        if QMessageBox.question(
                self, 'Remove holding',
                f'Remove {sym} from your portfolio?') == QMessageBox.StandardButton.Yes:
            del self._holdings[i]
            save_portfolio(self._holdings, self._cash)
            self._status.setText(f'Removed {sym}.')
            self._render()

    def _set_cash(self):
        value, ok = QInputDialog.getDouble(
            self, 'Set cash', 'Cash balance:', self._cash, 0, 1e12, 2)
        if ok:
            self._cash = value
            save_portfolio(self._holdings, self._cash)
            self._render()

    def _refresh_prices(self):
        symbols = [h['symbol'] for h in self._holdings]
        if not symbols:
            self._render()
            return
        if self._fetcher and self._fetcher.isRunning():
            return
        self._status.setText('Fetching latest prices …')
        self._fetcher = PriceFetcher(symbols)
        self._fetcher.prices_ready.connect(self._on_prices)
        self._fetcher.start()

    def _on_prices(self, prices: dict):
        self._prices = prices
        missing = [h['symbol'] for h in self._holdings if h['symbol'] not in prices]
        self._status.setText('Prices updated.' if not missing
                              else f'Prices updated. No data for: {", ".join(missing)}')
        self._render()

    # ── rendering ──
    def _cell(self, row, col, text, color=None, left=False):
        item = QTableWidgetItem(text)
        align = Qt.AlignmentFlag.AlignVCenter | (
            Qt.AlignmentFlag.AlignLeft if left else Qt.AlignmentFlag.AlignRight)
        item.setTextAlignment(align)
        if color:
            item.setForeground(QColor(color))
        self._table.setItem(row, col, item)

    def _render(self):
        result = compute_portfolio(self._holdings, self._prices, self._cash)
        rows = result['rows']
        self._table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            self._cell(r, 0, row['symbol'], left=True)
            self._cell(r, 1, f"{row['shares']:g}")
            self._cell(r, 2, f"{row['avg_cost']:,.2f}")
            self._cell(r, 5, f"{row['cost']:,.2f}")
            if row['price'] is None:
                for c in (3, 4, 6, 7):
                    self._cell(r, c, 'n/a', color=SUBTEXT)
            else:
                color = UP_COLOR if row['pnl'] >= 0 else DOWN_COLOR
                self._cell(r, 3, f"{row['price']:,.2f}")
                self._cell(r, 4, f"{row['mkt']:,.2f}")
                self._cell(r, 6, f"{row['pnl']:+,.2f}", color)
                self._cell(r, 7, f"{row['pnl_pct']:+.2f}%", color)

        tp  = result['total_pnl']
        col = UP_COLOR if tp >= 0 else DOWN_COLOR
        self._summary.setText(
            f"Invested {result['invested_cost']:,.2f}   |   "
            f"Cash {result['cash']:,.2f}   |   "
            f"Market Value {result['market_value']:,.2f}   |   "
            f"Total Value {result['total_value']:,.2f}   |   "
            f"P&L {tp:+,.2f} ({result['total_pnl_pct']:+.2f}%)"
        )
        self._summary.setStyleSheet(f'color: {col};')
        self._draw_pie(result)

    def _draw_pie(self, result):
        """Render an allocation pie (by market value of priced holdings + cash)."""
        if self._pie_canvas:
            self._pie_layout.removeWidget(self._pie_canvas)
            self._pie_canvas.deleteLater()
            self._pie_canvas = None

        labels, values = [], []
        for row in result['rows']:
            if row['mkt']:
                labels.append(row['symbol'])
                values.append(row['mkt'])
        if result['cash'] > 0:
            labels.append('CASH')
            values.append(result['cash'])
        if not values:
            return

        fig = Figure(figsize=(3.0, 3.4), facecolor=DARK_BG)
        ax = fig.add_subplot(111)
        wedges, _texts, autotexts = ax.pie(
            values, labels=labels, autopct='%1.0f%%', startangle=90,
            pctdistance=0.75, labeldistance=1.08,
            textprops={'color': TEXT, 'fontsize': 8},
            wedgeprops={'edgecolor': DARK_BG, 'linewidth': 1},
        )
        for at in autotexts:
            at.set_color('#0d0d1a')
            at.set_fontsize(7)
        ax.set_title('Allocation by value', color=TEXT, fontsize=10)
        ax.axis('equal')
        fig.tight_layout()

        self._pie_canvas = FigureCanvas(fig)
        self._pie_layout.addWidget(self._pie_canvas)
        self._pie_canvas.draw()


# ── AI insights dialog ────────────────────────────────────────────────────────

class InsightsDialog(QDialog):
    PRESETS = [
        ('Analyze my portfolio',  'Analyze my portfolio: concentration, diversification, and the biggest risks.'),
        ('Where am I overexposed?', 'Which positions or themes am I overexposed to, and how would I reduce that risk?'),
        ('Ideas from my watchlist', 'Given my current holdings, which names on my watchlist would diversify me, and why?'),
        ('Review my losers',       'Review my losing positions. For each, lay out the case to hold vs. cut.'),
    ]

    def __init__(self, parent, holdings: list, cash: float, watchlist: list):
        super().__init__(parent)
        self.setWindowTitle('AI Insights (portfolio-aware)')
        self.resize(720, 640)
        self.setStyleSheet(parent.styleSheet() if parent else '')

        self._holdings  = holdings
        self._cash      = cash
        self._watchlist = watchlist
        self._prices    = {}
        self._price_job = None
        self._insight_job = None

        self._build_ui()
        self._fetch_prices()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        presets = QHBoxLayout()
        for label, prompt in self.PRESETS:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _, p=prompt: self._ask(p))
            presets.addWidget(btn)
        layout.addLayout(presets)

        row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText('Ask anything about your portfolio…')
        self._input.returnPressed.connect(lambda: self._ask(self._input.text()))
        self._ask_btn = QPushButton('Analyze')
        self._ask_btn.setObjectName('AccentBtn')
        self._ask_btn.clicked.connect(lambda: self._ask(self._input.text()))
        row.addWidget(self._input)
        row.addWidget(self._ask_btn)
        layout.addLayout(row)

        self._answer = QTextEdit()
        self._answer.setReadOnly(True)
        self._answer.setPlaceholderText('The analysis will appear here.')
        layout.addWidget(self._answer)

        self._status = QLabel('')
        self._status.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        layout.addWidget(self._status)

        disclaimer = QLabel('⚠ Educational analysis only — not financial advice. '
                            'The model only sees the prices shown in the app.')
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet('color: #c9a227; font-size: 11px;')
        layout.addWidget(disclaimer)

    # ── prices for the snapshot ──
    def _fetch_prices(self):
        symbols = sorted({h['symbol'] for h in self._holdings} | set(self._watchlist))
        if not symbols:
            self._status.setText('No holdings or watchlist to analyze.')
            return
        self._status.setText('Fetching latest prices for the snapshot …')
        self._set_busy(True)
        self._price_job = PriceFetcher(symbols)
        self._price_job.prices_ready.connect(self._on_prices)
        self._price_job.start()

    def _on_prices(self, prices: dict):
        self._prices = prices
        self._set_busy(False)
        self._status.setText('Ready. Pick a preset or type a question.')

    # ── ask Claude ──
    def _ask(self, question: str):
        if self._insight_job and self._insight_job.isRunning():
            return
        if not self._prices:
            self._status.setText('Still fetching prices — try again in a moment.')
            return

        self._input.setText(question)
        portfolio = compute_portfolio(self._holdings, self._prices, self._cash)
        watchlist_quotes = {s: self._prices.get(s) for s in self._watchlist}

        self._answer.clear()
        self._status.setText('Thinking …')
        self._set_busy(True)

        self._insight_job = InsightsWorker(question, portfolio, watchlist_quotes)
        self._insight_job.chunk.connect(self._on_chunk)
        self._insight_job.done.connect(self._on_done)
        self._insight_job.error.connect(self._on_ai_error)
        self._insight_job.start()

    def _on_chunk(self, text: str):
        self._answer.moveCursor(self._answer.textCursor().MoveOperation.End)
        self._answer.insertPlainText(text)

    def _on_done(self):
        self._set_busy(False)
        self._status.setText('Done.')

    def _on_ai_error(self, msg: str):
        self._set_busy(False)
        if 'api_key' in msg.lower() or 'authentication' in msg.lower():
            msg = ('No Claude API key found. Set the ANTHROPIC_API_KEY environment '
                   'variable and reopen the app.\n\nDetails: ' + msg)
        self._status.setText('Error.')
        self._answer.setPlainText(msg)

    def _set_busy(self, busy: bool):
        self._ask_btn.setEnabled(not busy)
        self._input.setEnabled(not busy)


# ── price-alerts dialog ───────────────────────────────────────────────────────

class AlertsDialog(QDialog):
    HEADERS = ['Symbol', 'Condition', 'Price', 'Status']

    def __init__(self, parent, alerts: list, watchlist: list, on_change=None):
        super().__init__(parent)
        self.setWindowTitle('Price Alerts')
        self.resize(560, 460)
        self.setStyleSheet(parent.styleSheet() if parent else '')
        self._alerts = alerts          # shared list (mutated in place)
        self._watchlist = watchlist
        self._on_change = on_change
        self._build_ui()
        self._render()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QHBoxLayout()
        self._sym = QLineEdit()
        self._sym.setPlaceholderText('Symbol')
        if self._watchlist:
            comp = QCompleter(self._watchlist, self)
            comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            self._sym.setCompleter(comp)
        self._cond = QComboBox()
        self._cond.addItems(['above', 'below'])
        self._price = QLineEdit()
        self._price.setPlaceholderText('Price')
        add = QPushButton('+ Add')
        add.setObjectName('AccentBtn')
        add.clicked.connect(self._add)
        form.addWidget(self._sym, 2)
        form.addWidget(self._cond, 1)
        form.addWidget(self._price, 1)
        form.addWidget(add, 0)
        layout.addLayout(form)

        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

        btns = QHBoxLayout()
        rm = QPushButton('Remove')
        rm.clicked.connect(self._remove)
        rearm = QPushButton('Re-arm')
        rearm.clicked.connect(self._rearm)
        btns.addWidget(rm)
        btns.addWidget(rearm)
        btns.addStretch()
        layout.addLayout(btns)

        hint = QLabel('Alerts are checked on every price refresh (auto every 60s). '
                      'A fired alert must be re-armed before it can fire again.')
        hint.setWordWrap(True)
        hint.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        layout.addWidget(hint)

    def _render(self):
        self._table.setRowCount(len(self._alerts))
        for r, a in enumerate(self._alerts):
            triggered = a.get('triggered')
            cells = [
                a.get('symbol', ''),
                a.get('condition', ''),
                f"{a.get('price', 0):,.2f}",
                'TRIGGERED' if triggered else 'armed',
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 3:
                    item.setForeground(QColor(DOWN_COLOR if triggered else UP_COLOR))
                self._table.setItem(r, c, item)

    def _save(self):
        save_alerts(self._alerts)
        if self._on_change:
            self._on_change()
        self._render()

    def _add(self):
        sym = self._sym.text().strip().upper()
        if not sym:
            QMessageBox.warning(self, 'Invalid', 'Symbol is required.')
            return
        try:
            price = float(self._price.text())
        except ValueError:
            QMessageBox.warning(self, 'Invalid', 'Price must be a number.')
            return
        self._alerts.append({
            'symbol': sym, 'condition': self._cond.currentText(),
            'price': price, 'enabled': True, 'triggered': False,
        })
        self._sym.clear()
        self._price.clear()
        self._save()

    def _selected_row(self):
        rows = self._table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _remove(self):
        i = self._selected_row()
        if 0 <= i < len(self._alerts):
            del self._alerts[i]
            self._save()

    def _rearm(self):
        i = self._selected_row()
        if 0 <= i < len(self._alerts):
            self._alerts[i]['triggered'] = False
            self._save()


# ── main window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Stock Monitor')
        self.setMinimumSize(1320, 760)

        self._watchlist  = self._load_watchlist()
        self._fetcher    = None
        self._quote_job  = None
        self._search_job = None
        self._selected   = None
        self._period_key = '1M'
        self._indicators = set()       # active overlays: BBANDS / RSI / MACD
        self._rows       = {}          # symbol -> StockRow
        self._sugg_map   = {}          # display string -> symbol
        self._alerts     = load_alerts()

        self._build_ui()
        self._apply_theme()
        self._refresh_list()
        self._refresh_quotes()

        # auto-refresh quotes every 60s (cached layer keeps this cheap)
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(60_000)
        self._auto_timer.timeout.connect(self._refresh_quotes)
        self._auto_timer.start()

    # ── persistence ──
    def _load_watchlist(self) -> list:
        if os.path.exists(WATCHLIST_FILE):
            try:
                with open(WATCHLIST_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_watchlist(self):
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(self._watchlist, f, indent=2)

    # ── UI ──
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)
        root_layout.addWidget(self._build_left_panel(), 0)
        root_layout.addWidget(self._build_right_panel(), 1)

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFixedWidth(260)
        panel.setObjectName('LeftPanel')
        layout = QVBoxLayout(panel)
        layout.setSpacing(6)

        title = QLabel('Watchlist')
        title.setFont(QFont('Segoe UI', 12, QFont.Weight.Bold))
        layout.addWidget(title)

        self._input = QLineEdit()
        self._input.setPlaceholderText('Company name or symbol…')
        self._input.returnPressed.connect(self._add_stock)
        self._input.textEdited.connect(self._on_text_edited)
        layout.addWidget(self._input)

        # autocomplete
        self._completer = QCompleter(self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer_model = QStringListModel(self)
        self._completer.setModel(self._completer_model)
        self._completer.activated[str].connect(self._on_suggestion_picked)
        self._input.setCompleter(self._completer)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._run_search)

        add_btn = QPushButton('+ Add')
        add_btn.setObjectName('AccentBtn')
        add_btn.clicked.connect(self._add_stock)
        layout.addWidget(add_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName('Separator')
        layout.addWidget(sep)

        self._list = QListWidget()
        self._list.setObjectName('StockList')
        self._list.itemClicked.connect(self._on_stock_clicked)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        rm_btn = QPushButton('Remove')
        rm_btn.clicked.connect(self._remove_stock)
        ref_btn = QPushButton('↻ Refresh')
        ref_btn.clicked.connect(self._hard_refresh)
        btn_row.addWidget(rm_btn)
        btn_row.addWidget(ref_btn)
        layout.addLayout(btn_row)

        pf_btn = QPushButton('📊 Portfolio / P&L')
        pf_btn.setObjectName('AccentBtn')
        pf_btn.clicked.connect(self._open_portfolio)
        layout.addWidget(pf_btn)

        ai_btn = QPushButton('💡 AI Insights')
        ai_btn.setObjectName('AccentBtn')
        ai_btn.clicked.connect(self._open_insights)
        layout.addWidget(ai_btn)

        alert_btn = QPushButton('🔔 Price Alerts')
        alert_btn.setObjectName('AccentBtn')
        alert_btn.clicked.connect(self._open_alerts)
        layout.addWidget(alert_btn)

        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(self._build_period_bar())
        self._chart = ChartPanel()
        layout.addWidget(self._chart)
        self._status = QLabel('Ready — add a stock and click it to load a chart.')
        self._status.setObjectName('StatusBar')
        layout.addWidget(self._status)
        return panel

    def _build_period_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self._period_btns = {}
        for label in PERIODS:
            btn = QPushButton(label)
            btn.setFixedSize(52, 28)
            btn.setObjectName('PeriodBtn')
            btn.setCheckable(True)
            btn.setChecked(label == self._period_key)
            btn.clicked.connect(lambda _, l=label: self._set_period(l))
            bar.addWidget(btn)
            self._period_btns[label] = btn
        bar.addStretch()

        # indicator toggles on the right
        self._ind_btns = {}
        for key, label in [('BBANDS', 'BB'), ('RSI', 'RSI'), ('MACD', 'MACD')]:
            btn = QPushButton(label)
            btn.setFixedSize(56, 28)
            btn.setObjectName('PeriodBtn')
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, k=key: self._toggle_indicator(k))
            bar.addWidget(btn)
            self._ind_btns[key] = btn
        return bar

    def _toggle_indicator(self, key: str):
        if key in self._indicators:
            self._indicators.discard(key)
        else:
            self._indicators.add(key)
        self._ind_btns[key].setChecked(key in self._indicators)
        if self._selected:
            self._fetch()

    # ── autocomplete ──
    def _on_text_edited(self, _text: str):
        self._search_timer.start()   # debounce

    def _run_search(self):
        query = self._input.text().strip()
        if len(query) < 2:
            return
        if self._search_job and self._search_job.isRunning():
            self._search_job.terminate()
            self._search_job.wait()
        self._search_job = SearchWorker(query)
        self._search_job.results_ready.connect(self._on_search_results)
        self._search_job.start()

    def _on_search_results(self, results: list):
        self._sugg_map = {disp: sym for disp, sym in results}
        self._completer_model.setStringList(list(self._sugg_map.keys()))
        if results:
            self._completer.complete()

    def _on_suggestion_picked(self, display: str):
        sym = self._sugg_map.get(display)
        if sym:
            # defer so the completer finishes writing the text first
            QTimer.singleShot(0, lambda: self._input.setText(sym))

    # ── watchlist ──
    def _refresh_list(self):
        self._list.clear()
        self._rows = {}
        for sym in self._watchlist:
            item = QListWidgetItem(self._list)
            item.setData(Qt.ItemDataRole.UserRole, sym)
            item.setSizeHint(self._row_hint())
            row = StockRow(sym)
            self._rows[sym] = row
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
        self._sync_alarms()

    def _row_hint(self):
        from PyQt6.QtCore import QSize
        return QSize(0, 40)

    def _add_stock(self):
        text = self._input.text().strip()
        # accept "AAPL — Apple Inc." style picks too
        sym = self._sugg_map.get(text, text).upper()
        if not sym:
            return
        if sym in self._watchlist:
            QMessageBox.information(self, 'Already added', f'{sym} is already in your watchlist.')
            return
        self._watchlist.append(sym)
        self._save_watchlist()
        self._refresh_list()
        self._input.clear()
        self._status.setText(f'Added {sym}.')
        self._refresh_quotes()

    def _remove_stock(self):
        item = self._list.currentItem()
        if not item:
            return
        sym = item.data(Qt.ItemDataRole.UserRole)
        self._watchlist.remove(sym)
        self._save_watchlist()
        self._refresh_list()
        if self._selected == sym:
            self._selected = None
        self._status.setText(f'Removed {sym}.')

    # ── quotes ──
    def _refresh_quotes(self):
        if not self._watchlist:
            return
        if self._quote_job and self._quote_job.isRunning():
            return
        self._quote_job = QuoteFetcher(list(self._watchlist))
        self._quote_job.quote_ready.connect(self._on_quote)
        self._quote_job.start()

    def _on_quote(self, sym: str, price: float, pct: float):
        row = self._rows.get(sym)
        if row:
            row.set_quote(price, pct)
        self._check_alerts(sym, price)

    # ── price alerts ──
    def _open_alerts(self):
        AlertsDialog(self, self._alerts, list(self._watchlist),
                     on_change=self._sync_alarms).exec()
        self._sync_alarms()

    def _check_alerts(self, sym: str, price: float):
        fired = evaluate_alerts(self._alerts, sym, price)
        if not fired:
            return
        save_alerts(self._alerts)
        QApplication.beep()
        lines = [f"{a['symbol']} went {a['condition']} {a['price']:.2f}  (now {price:.2f})"
                 for a in fired]
        for a in fired:
            row = self._rows.get(a['symbol'])
            if row:
                row.set_alarm(True)
        self._status.setText('🔔 ALERT — ' + '; '.join(lines))
        QMessageBox.information(self, 'Price alert', '\n'.join(lines))

    def _sync_alarms(self):
        """Re-apply the row highlight based on which alerts are currently triggered."""
        for sym, row in self._rows.items():
            active = any(a.get('triggered') and a.get('symbol') == sym for a in self._alerts)
            row.set_alarm(active)

    def _hard_refresh(self):
        """Bypass the cache: re-fetch quotes and the current chart from source."""
        data.clear_cache()
        self._refresh_quotes()
        if self._selected:
            self._fetch()
        self._status.setText('Refreshed from source.')

    # ── portfolio ──
    def _open_portfolio(self):
        PortfolioDialog(self).exec()

    # ── AI insights ──
    def _open_insights(self):
        holdings, cash = load_portfolio()
        InsightsDialog(self, holdings, cash, list(self._watchlist)).exec()

    # ── chart ──
    def _on_stock_clicked(self, item):
        self._selected = item.data(Qt.ItemDataRole.UserRole)
        self._fetch()

    def _set_period(self, key: str):
        self._period_key = key
        for k, btn in self._period_btns.items():
            btn.setChecked(k == key)
        if self._selected:
            self._fetch()

    def _fetch(self):
        period, interval = PERIODS[self._period_key]
        self._status.setText(f'Loading {self._selected} [{self._period_key}] …')
        if self._fetcher and self._fetcher.isRunning():
            self._fetcher.terminate()
            self._fetcher.wait()
        self._fetcher = DataFetcher(self._selected, period, interval)
        self._fetcher.data_ready.connect(self._on_data)
        self._fetcher.error.connect(self._on_error)
        self._fetcher.start()

    def _on_data(self, df, symbol: str):
        self._chart.render(df, symbol, self._period_key, self._indicators)
        self._status.setText(
            f'{symbol}  [{self._period_key}]  —  {len(df)} bars  |  '
            f'Last close: {df["Close"].iloc[-1]:.2f}'
        )

    def _on_error(self, msg: str):
        self._status.setText(f'Error: {msg}')
        QMessageBox.warning(self, 'Fetch error', msg)

    # ── theme ──
    def _apply_theme(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {DARK_BG}; color: {TEXT};
                font-family: 'Segoe UI'; font-size: 13px;
            }}
            QFrame#LeftPanel {{
                background-color: {PANEL_BG}; border-radius: 8px;
                border: 1px solid #2a2a4a;
            }}
            QFrame#Separator {{ color: #2a2a4a; }}
            QListWidget#StockList {{
                background-color: {DARK_BG}; border: 1px solid #2a2a4a;
                border-radius: 6px; padding: 4px;
            }}
            QListWidget#StockList::item {{ border-radius: 4px; margin: 1px 0; }}
            QListWidget#StockList::item:hover {{ background-color: {ACCENT}; }}
            QListWidget#StockList::item:selected {{ background-color: {ACCENT}; }}
            QLineEdit {{
                background-color: {DARK_BG}; border: 1px solid #2a2a4a;
                border-radius: 6px; padding: 6px 8px; color: {TEXT};
            }}
            QLineEdit:focus {{ border: 1px solid {HIGHLIGHT}; }}
            QPushButton {{
                background-color: {ACCENT}; border: 1px solid #2a2a4a;
                border-radius: 6px; padding: 6px 10px; color: {TEXT};
            }}
            QPushButton:hover {{ background-color: #1a4a80; }}
            QPushButton:pressed {{ background-color: {HIGHLIGHT}; }}
            QPushButton#AccentBtn {{
                background-color: {HIGHLIGHT}; border: none; font-weight: bold;
            }}
            QPushButton#AccentBtn:hover {{ background-color: #c73652; }}
            QPushButton#PeriodBtn {{
                background-color: {ACCENT}; border-radius: 5px;
                font-size: 11px; padding: 0;
            }}
            QPushButton#PeriodBtn:checked {{
                background-color: {HIGHLIGHT}; color: #fff; font-weight: bold;
            }}
            QLabel {{ color: {TEXT}; }}
            QLabel#StatusBar {{ color: {SUBTEXT}; font-size: 11px; padding: 2px 4px; }}
            QListView {{
                background-color: {PANEL_BG}; color: {TEXT};
                border: 1px solid {HIGHLIGHT}; selection-background-color: {ACCENT};
            }}
        """)


def _load_dotenv():
    """
    Load KEY=VALUE pairs from a .env file next to this script into os.environ.

    Lets the app find ANTHROPIC_API_KEY regardless of how it's launched, instead
    of relying on a session-local `set` in one specific terminal. Existing
    environment variables take precedence (a real env var overrides the file).
    """
    path = os.path.join(_APP_DIR, '.env')
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


def main():
    _load_dotenv()
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
