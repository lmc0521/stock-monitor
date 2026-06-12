"""Feature pages (embedded) and the small pop-up dialogs."""

import matplotlib
matplotlib.use('QtAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QListWidgetItem, QLineEdit, QLabel,
    QMessageBox, QSizePolicy, QFrame, QCompleter, QDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QFileDialog, QTextEdit,
    QFormLayout, QInputDialog, QComboBox, QProgressBar, QScrollArea, QStackedWidget
)
from PyQt6.QtCore import Qt, QTimer, QStringListModel
from PyQt6.QtGui import QFont, QColor

import history
import ledger
import thirteenf
import sentiment
import llm
from theme import (DARK_BG, PANEL_BG, ACCENT, HIGHLIGHT, TEXT, SUBTEXT,
                   UP_COLOR, DOWN_COLOR)
from appstate import (compute_portfolio, load_portfolio, save_portfolio,
                      parse_portfolio_csv, save_alerts)
from workers import (PriceFetcher, SearchWorker, InsightsWorker, SentimentWorker,
                     HistoryWorker, ThirteenFWorker, StrategyWorker, IPOWorker,
                     RumoredIPOWorker, AnalysisWorker)

class AddHoldingDialog(QDialog):
    """Add a single holding, with company-name autocomplete and symbol validation."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Add holding')
        self.setStyleSheet(parent.window().styleSheet() if parent else '')
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
        self._date   = QLineEdit(); self._date.setPlaceholderText('YYYY-MM-DD (optional, for history)')
        form.addRow('Symbol', self._sym)
        form.addRow('Shares', self._shares)
        form.addRow('Avg cost', self._cost)
        form.addRow('Buy date', self._date)

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

        # optional buy date for history reconstruction
        raw_date = self._date.text().strip()
        buy_date = None
        if raw_date:
            try:
                from datetime import datetime as _dt
                buy_date = _dt.strptime(raw_date, '%Y-%m-%d').date().isoformat()
            except ValueError:
                QMessageBox.warning(self, 'Invalid', 'Buy date must be YYYY-MM-DD (or left blank).')
                return

        # validate the symbol actually has market data before accepting
        self._pending = {'symbol': sym, 'shares': shares, 'avg_cost': cost}
        if buy_date:
            self._pending['date'] = buy_date
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


class PortfolioPage(QWidget):
    HEADERS = ['Symbol', 'Shares', 'Avg Cost', 'Price',
               'Mkt Value', 'Cost', 'P&L', 'P&L %']

    def __init__(self, parent=None):
        super().__init__(parent)
        self._holdings, self._cash = [], 0.0
        self._prices = {}
        self._fx = {}
        self._fetcher = None
        self._build_ui()

    def on_show(self):
        self._holdings, self._cash = load_portfolio()
        if self._holdings:
            self._refresh_prices()
        else:
            self._render()
            self._status.setText('No holdings yet — add them in Transactions, '
                                 'use "+ Add holding", or "Import CSV…".')

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
        self._fetcher = PriceFetcher(symbols, with_fx=True)
        self._fetcher.quotes_ready.connect(self._on_quotes)
        self._fetcher.start()

    def _on_quotes(self, prices: dict, fx: dict):
        self._prices = prices
        self._fx = fx
        missing = [h['symbol'] for h in self._holdings if h['symbol'] not in prices]
        non_usd = sorted({c for c, _r in fx.values() if c != 'USD'})
        note = f' Converted to USD from: {", ".join(non_usd)}.' if non_usd else ''
        self._status.setText(('Prices updated.' if not missing
                              else f'Prices updated. No data for: {", ".join(missing)}.')
                             + note)
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
        result = compute_portfolio(self._holdings, self._prices, self._cash,
                                   fx=self._fx)
        rows = result['rows']
        self._table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            cur = row.get('currency', 'USD')
            sym_label = row['symbol'] if cur == 'USD' else f"{row['symbol']}  ({cur})"
            self._cell(r, 0, sym_label, left=True)
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

        # record today's value to the running history (only when prices are loaded)
        if result['market_value'] > 0:
            try:
                history.record_snapshot(result['total_value'], result['market_value'],
                                        result['cash'], result['invested_cost'])
            except Exception:
                pass

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

class InsightsPage(QWidget):
    PRESETS = [
        ('Analyze my portfolio',  'Analyze my portfolio: concentration, diversification, and the biggest risks.'),
        ('Where am I overexposed?', 'Which positions or themes am I overexposed to, and how would I reduce that risk?'),
        ('Ideas from my watchlist', 'Given my current holdings, which names on my watchlist would diversify me, and why?'),
        ('Review my losers',       'Review my losing positions. For each, lay out the case to hold vs. cut.'),
    ]

    def __init__(self, parent=None, main=None):
        super().__init__(parent)
        self._main = main
        self._holdings  = []
        self._cash      = 0.0
        self._watchlist = []
        self._prices    = {}
        self._price_job = None
        self._insight_job = None
        self._build_ui()

    def on_show(self):
        self._holdings, self._cash = load_portfolio()
        self._watchlist = list(self._main._watchlist) if self._main else []
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
                            'Uses live web search; verify anything important yourself.')
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
        low = msg.lower()
        if 'api_key' in low or 'authentication' in low or 'no claude api key' in low:
            friendly = ('No Claude API key found. Put a .env file with '
                        'ANTHROPIC_API_KEY=... next to the app (or set it as an '
                        'environment variable), then reopen.')
        elif 'connection' in low or 'timeout' in low or 'timed out' in low:
            friendly = ('Could not reach Claude after several retries. Check your '
                        'internet connection and try again. Web-search queries take '
                        'longer — if it keeps failing, try a simpler question.')
        elif 'rate' in low or 'overload' in low or '429' in low or '529' in low:
            friendly = ('Claude is busy or rate-limited right now. Wait a few '
                        'seconds and try again.')
        else:
            friendly = 'Something went wrong talking to Claude.'

        self._status.setText('Error.')
        full = f'{friendly}\n\nDetails: {msg}'
        # preserve any partial answer already streamed
        if self._answer.toPlainText().strip():
            self._answer.append('\n\n— ' + friendly)
        else:
            self._answer.setPlainText(full)

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
        self.setStyleSheet(parent.window().styleSheet() if parent else '')
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
        self._cond.addItems(['above', 'below', 'moves ±%'])
        self._cond.currentIndexChanged.connect(
            lambda i: self._price.setPlaceholderText('Percent' if i == 2 else 'Price'))
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
            is_move = a.get('condition') == 'move'
            cells = [
                a.get('symbol', ''),
                'moves ±%' if is_move else a.get('condition', ''),
                f"±{a.get('price', 0):g}%" if is_move else f"{a.get('price', 0):,.2f}",
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
            QMessageBox.warning(self, 'Invalid', 'Price / percent must be a number.')
            return
        cond = 'move' if self._cond.currentIndex() == 2 else self._cond.currentText()
        if cond == 'move' and price <= 0:
            QMessageBox.warning(self, 'Invalid', 'Percent threshold must be positive.')
            return
        self._alerts.append({
            'symbol': sym, 'condition': cond,
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


# ── market-sentiment dialog ───────────────────────────────────────────────────

class SentimentPage(QWidget):
    """Investor-emotion indices: CNN Fear & Greed (+components), crypto F&G, VIX."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._job = None
        self._loaded = False
        self._build_ui()

    def on_show(self):
        if not self._loaded:
            self._loaded = True
            self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        head = QHBoxLayout()
        title = QLabel('Investor Sentiment')
        title.setFont(QFont('Segoe UI', 14, QFont.Weight.Bold))
        ref = QPushButton('↻ Refresh')
        ref.clicked.connect(self._refresh)
        head.addWidget(title); head.addStretch(); head.addWidget(ref)
        layout.addLayout(head)

        # scrollable body so all components fit
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._body = QVBoxLayout(inner)
        self._body.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        self._status = QLabel('')
        self._status.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        layout.addWidget(self._status)
        disc = QLabel('Sources: CNN Business, alternative.me, Yahoo Finance. '
                      'Scores are 0 (extreme fear) to 100 (extreme greed). For information only.')
        disc.setWordWrap(True)
        disc.setStyleSheet(f'color: {SUBTEXT}; font-size: 10px;')
        layout.addWidget(disc)

    # ── helpers ──
    def _clear(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                self._clear(item.layout())

    def _bar(self, score: float) -> QProgressBar:
        _, color = sentiment.classify(score)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(int(round(score)))
        bar.setTextVisible(False)
        bar.setFixedHeight(14)
        bar.setStyleSheet(
            'QProgressBar{background:#0d0d1a;border:1px solid #2a2a4a;border-radius:6px;}'
            f'QProgressBar::chunk{{background-color:{color};border-radius:6px;}}')
        return bar

    def _add_headline(self, name: str, score: float):
        label, color = sentiment.classify(score)
        t = QLabel(name); t.setFont(QFont('Segoe UI', 12, QFont.Weight.Bold))
        self._body.addWidget(t)
        v = QLabel(f'{score:.0f}  —  {label}')
        v.setFont(QFont('Segoe UI', 18, QFont.Weight.Bold))
        v.setStyleSheet(f'color: {color};')
        self._body.addWidget(v)
        self._body.addWidget(self._bar(score))
        self._body.addSpacing(10)

    def _add_component(self, name: str, score: float):
        _, color = sentiment.classify(score)
        row = QHBoxLayout()
        n = QLabel(name); n.setFixedWidth(210)
        val = QLabel(f'{score:.0f}'); val.setFixedWidth(32)
        val.setStyleSheet(f'color: {color};')
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(n); row.addWidget(self._bar(score)); row.addWidget(val)
        self._body.addLayout(row)

    def _add_note(self, text: str):
        n = QLabel(text); n.setStyleSheet(f'color: {SUBTEXT};')
        self._body.addWidget(n)

    def _add_vix(self, vix):
        mood, color = sentiment.vix_mood(vix)
        self._body.addSpacing(12)
        t = QLabel('VIX — Volatility Index ("fear gauge")')
        t.setFont(QFont('Segoe UI', 12, QFont.Weight.Bold))
        self._body.addWidget(t)
        txt = 'unavailable' if vix is None else f'{vix:.2f}  —  {mood}'
        v = QLabel(txt); v.setFont(QFont('Segoe UI', 16, QFont.Weight.Bold))
        v.setStyleSheet(f'color: {color};')
        self._body.addWidget(v)
        note = QLabel('Lower = calm markets, higher = fear / turbulence.')
        note.setStyleSheet(f'color: {SUBTEXT}; font-size: 10px;')
        self._body.addWidget(note)

    def _add_buffett(self, b):
        self._body.addSpacing(12)
        t = QLabel('Buffett Indicator — US market cap ÷ GDP')
        t.setFont(QFont('Segoe UI', 12, QFont.Weight.Bold))
        self._body.addWidget(t)
        if not b:
            self._add_note('unavailable right now.')
            return
        v = QLabel(f"{b['ratio']:.0f}%  —  {b['label']}")
        v.setFont(QFont('Segoe UI', 16, QFont.Weight.Bold))
        v.setStyleSheet(f"color: {b['color']};")
        self._body.addWidget(v)
        note = QLabel(f"Source: World Bank, {b['year']} (annual).  "
                      "<90% cheap · ~100% fair · >150% richly valued.")
        note.setStyleSheet(f'color: {SUBTEXT}; font-size: 10px;')
        self._body.addWidget(note)

    # ── data ──
    def _refresh(self):
        if self._job and self._job.isRunning():
            return
        self._status.setText('Fetching sentiment data …')
        self._job = SentimentWorker()
        self._job.ready.connect(self._on_data)
        self._job.start()

    def _on_data(self, d: dict):
        self._clear(self._body)
        cnn, crypto, vix = d.get('cnn'), d.get('crypto'), d.get('vix')

        if cnn and cnn.get('score') is not None:
            self._add_headline('CNN Fear & Greed Index', cnn['score'])
            for c in cnn.get('components', []):
                self._add_component(c['label'], c['score'])
        else:
            self._add_note('CNN Fear & Greed unavailable right now.')

        self._body.addSpacing(12)
        if crypto and crypto.get('score') is not None:
            self._add_headline('Crypto Fear & Greed Index', crypto['score'])
        else:
            self._add_note('Crypto Fear & Greed unavailable right now.')

        self._add_vix(vix)
        self._add_buffett(d.get('buffett'))

        errs = d.get('errors')
        self._status.setText('Updated.' if not errs
                             else 'Updated — some sources failed: ' + '; '.join(errs)[:140])


# ── transaction ledger dialog ─────────────────────────────────────────────────

class LedgerPage(QWidget):
    HEADERS = ['Date', 'Symbol', 'Type', 'Shares', 'Price / Amount']

    def __init__(self, parent=None):
        super().__init__(parent)
        self._txns = []
        self._build_ui()

    def on_show(self):
        self._txns = ledger.load_transactions()
        self._render()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QHBoxLayout()
        from datetime import date as _date
        self._date = QLineEdit(); self._date.setPlaceholderText('YYYY-MM-DD')
        self._date.setText(_date.today().isoformat()); self._date.setFixedWidth(110)
        self._sym = QLineEdit(); self._sym.setPlaceholderText('Symbol'); self._sym.setFixedWidth(90)
        self._type = QComboBox(); self._type.addItems(['buy', 'sell', 'dividend'])
        self._shares = QLineEdit(); self._shares.setPlaceholderText('Shares'); self._shares.setFixedWidth(80)
        self._price = QLineEdit(); self._price.setPlaceholderText('Price / Amount'); self._price.setFixedWidth(110)
        add = QPushButton('+ Add'); add.setObjectName('AccentBtn'); add.clicked.connect(self._add)
        for w in (self._date, self._sym, self._type, self._shares, self._price, add):
            form.addWidget(w)
        layout.addLayout(form)

        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

        rm = QPushButton('Remove selected')
        rm.clicked.connect(self._remove)
        layout.addWidget(rm)

        self._summary = QLabel('')
        self._summary.setFont(QFont('Segoe UI', 11, QFont.Weight.Bold))
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        note = QLabel('Buys/sells/dividends here are the source of truth: open '
                      'positions are synced to the Portfolio page, and the History '
                      'chart reconstructs from them. Realized P&L uses average cost.')
        note.setWordWrap(True)
        note.setStyleSheet(f'color: {SUBTEXT}; font-size: 10px;')
        layout.addWidget(note)

    def _add(self):
        from datetime import datetime as _dt
        raw_date = self._date.text().strip()
        try:
            date = _dt.strptime(raw_date, '%Y-%m-%d').date().isoformat()
        except ValueError:
            QMessageBox.warning(self, 'Invalid', 'Date must be YYYY-MM-DD.')
            return
        sym = self._sym.text().strip().upper()
        if not sym:
            QMessageBox.warning(self, 'Invalid', 'Symbol is required.')
            return
        typ = self._type.currentText()
        try:
            price = float(self._price.text())
        except ValueError:
            QMessageBox.warning(self, 'Invalid', 'Price / Amount must be a number.')
            return

        if typ == 'dividend':
            txn = {'date': date, 'symbol': sym, 'type': 'dividend', 'amount': price}
        else:
            try:
                shares = float(self._shares.text())
            except ValueError:
                QMessageBox.warning(self, 'Invalid', 'Shares must be a number.')
                return
            if shares <= 0:
                QMessageBox.warning(self, 'Invalid', 'Shares must be positive.')
                return
            txn = {'date': date, 'symbol': sym, 'type': typ, 'shares': shares, 'price': price}

        self._txns.append(txn)
        self._commit()
        self._sym.clear(); self._shares.clear(); self._price.clear()

    def _remove(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        i = rows[0].row()
        if 0 <= i < len(self._txns):
            del self._txns[i]
            self._commit()

    def _commit(self):
        ledger.save_transactions(self._txns)
        self._txns = ledger.load_transactions()         # canonical sorted order
        # sync open positions into the portfolio (preserve existing cash)
        _, cash = load_portfolio()
        save_portfolio(ledger.current_holdings(self._txns), cash)
        self._render()

    def _render(self):
        self._table.setRowCount(len(self._txns))
        for r, t in enumerate(self._txns):
            is_div = t.get('type') == 'dividend'
            cells = [
                t.get('date', ''),
                t.get('symbol', ''),
                t.get('type', ''),
                '' if is_div else f"{float(t.get('shares', 0)):g}",
                f"{float(t.get('amount' if is_div else 'price', 0)):,.2f}",
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | (
                    Qt.AlignmentFlag.AlignLeft if c < 3 else Qt.AlignmentFlag.AlignRight))
                if c == 2 and t.get('type') in ('buy', 'sell', 'dividend'):
                    item.setForeground(QColor({'buy': UP_COLOR, 'sell': DOWN_COLOR,
                                               'dividend': '#c9a227'}[t['type']]))
                self._table.setItem(r, c, item)

        s = ledger.summary(self._txns)
        color = UP_COLOR if s['realized'] >= 0 else DOWN_COLOR
        self._summary.setText(
            f"Realized P&L: {s['realized']:+,.2f}    |    "
            f"Dividends: {s['dividends']:,.2f}    |    "
            f"Open positions: {s['open_positions']}")
        self._summary.setStyleSheet(f'color: {color};')


# ── portfolio-history dialog ──────────────────────────────────────────────────

class HistoryPage(QWidget):
    """Line chart of portfolio value over time (reconstructed + recorded)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._holdings = []
        self._cash = 0.0
        self._job = None
        self._canvas = None
        self._build_ui()

    def on_show(self):
        self._holdings, self._cash = load_portfolio()
        self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel('Portfolio Value History')
        title.setFont(QFont('Segoe UI', 13, QFont.Weight.Bold))
        ref = QPushButton('↻ Refresh')
        ref.clicked.connect(self._refresh)
        head.addWidget(title); head.addStretch(); head.addWidget(ref)
        layout.addLayout(head)

        self._chart_holder = QVBoxLayout()
        layout.addLayout(self._chart_holder, 1)

        self._status = QLabel('')
        self._status.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        layout.addWidget(self._status)
        note = QLabel('Reconstructed from purchase dates + historical prices; daily '
                      'snapshots are recorded going forward. Holdings without a buy '
                      'date are assumed held over the past year.')
        note.setWordWrap(True)
        note.setStyleSheet(f'color: {SUBTEXT}; font-size: 10px;')
        layout.addWidget(note)

    def _refresh(self):
        if not self._holdings:
            self._status.setText('No holdings yet — add some in the Portfolio page.')
            return
        if self._job and self._job.isRunning():
            return
        self._status.setText('Building history …')
        self._job = HistoryWorker(self._holdings, self._cash)
        self._job.ready.connect(self._on_ready)
        self._job.error.connect(lambda m: self._status.setText('Error: ' + m))
        self._job.start()

    def _on_ready(self, total, invested):
        if total is None or len(total) == 0:
            self._status.setText('Not enough price history to build a chart.')
            return
        self._draw(total, invested)
        first, last = float(total.iloc[0]), float(total.iloc[-1])
        ret = (last - first) / first * 100 if first else 0.0
        self._status.setText(
            f'{len(total)} days  |  {total.index[0].date()}: {first:,.0f}  →  '
            f'now: {last:,.0f}   ({ret:+.2f}%)')

    def _draw(self, total, invested):
        if self._canvas:
            self._chart_holder.removeWidget(self._canvas)
            self._canvas.deleteLater()
            self._canvas = None

        fig = Figure(figsize=(9, 4.6), facecolor=DARK_BG)
        ax = fig.add_subplot(111)
        ax.set_facecolor('#0d0d1a')
        x = total.index
        ax.plot(x, total.values, color=UP_COLOR, lw=1.6, label='Portfolio value')
        if invested is not None and len(invested):
            inv = invested.reindex(x).ffill()
            ax.plot(x, inv.values, color='#7e8aa2', lw=1.0, ls='--', label='Invested (cost basis)')
            ax.fill_between(x, inv.values, total.values,
                            where=(total.values >= inv.values),
                            color=UP_COLOR, alpha=0.12, interpolate=True)
            ax.fill_between(x, inv.values, total.values,
                            where=(total.values < inv.values),
                            color=DOWN_COLOR, alpha=0.12, interpolate=True)
        ax.set_title('Portfolio Value Over Time', color=TEXT, fontsize=11)
        ax.tick_params(colors=SUBTEXT)
        for sp in ax.spines.values():
            sp.set_color('#2a2a4a')
        ax.grid(True, color='#2a2a4a', ls=':', lw=0.5)
        leg = ax.legend(facecolor=PANEL_BG, edgecolor='#2a2a4a', fontsize=9)
        for t in leg.get_texts():
            t.set_color(TEXT)
        fig.autofmt_xdate()
        fig.tight_layout()

        self._canvas = FigureCanvas(fig)
        self._chart_holder.addWidget(self._canvas)
        self._canvas.draw()


# ── 13F holdings dialog ───────────────────────────────────────────────────────

class ThirteenFPage(QWidget):
    HEADERS = ['#', 'Issuer', 'Class', 'Value', '% Port', 'Shares']
    MAX_ROWS = 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self._job = None
        self._loaded = False
        self._build_ui()

    def on_show(self):
        if not self._loaded:
            self._loaded = True
            self._load()             # auto-load the first preset on first view

    @staticmethod
    def _money(v: float) -> str:
        v = float(v)
        if abs(v) >= 1e9:
            return f'${v/1e9:.2f}B'
        if abs(v) >= 1e6:
            return f'${v/1e6:.1f}M'
        return f'${v:,.0f}'

    def _build_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self._combo = QComboBox()
        for name in thirteenf.KNOWN_FUNDS:
            self._combo.addItem(name)
        self._cik = QLineEdit()
        self._cik.setPlaceholderText('or custom CIK')
        self._cik.setFixedWidth(130)
        self._cik.returnPressed.connect(self._load)
        load = QPushButton('Load')
        load.setObjectName('AccentBtn')
        load.clicked.connect(self._load)
        top.addWidget(self._combo, 1)
        top.addWidget(self._cik)
        top.addWidget(load)
        layout.addLayout(top)

        self._header = QLabel('')
        self._header.setWordWrap(True)
        self._header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._header)

        mid = QHBoxLayout()
        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)   # Issuer stretches
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        mid.addWidget(self._table, 3)

        pie_box = QWidget()
        pie_box.setFixedWidth(320)
        self._pie_layout = QVBoxLayout(pie_box)
        self._pie_layout.setContentsMargins(0, 0, 0, 0)
        self._pie_canvas = None
        mid.addWidget(pie_box, 0)
        layout.addLayout(mid)

        self._status = QLabel('')
        self._status.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        layout.addWidget(self._status)
        disc = QLabel('Source: SEC EDGAR 13F-HR filings. Filed quarterly with a '
                      '~45-day delay — these are last-quarter positions, not live. '
                      'If a preset shows no data, enter the manager\'s CIK.')
        disc.setWordWrap(True)
        disc.setStyleSheet(f'color: {SUBTEXT}; font-size: 10px;')
        layout.addWidget(disc)

    def _cell(self, row, col, text, left=False):
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | (
            Qt.AlignmentFlag.AlignLeft if left else Qt.AlignmentFlag.AlignRight))
        self._table.setItem(row, col, item)

    def _load(self):
        if self._job and self._job.isRunning():
            return
        cik = self._cik.text().strip() or thirteenf.KNOWN_FUNDS.get(self._combo.currentText())
        if not cik:
            return
        self._header.setText('')
        self._status.setText('Fetching from SEC EDGAR …')
        self._job = ThirteenFWorker(cik)
        self._job.ready.connect(self._on_ready)
        self._job.error.connect(lambda m: self._status.setText('Error: ' + m))
        self._job.start()

    def _on_ready(self, d: dict):
        self._header.setText(
            f"<b>{d['fund']}</b> (CIK {d['cik']}) &nbsp;—&nbsp; as of "
            f"<b>{d['report_date']}</b>, filed {d['filing_date']} &nbsp;|&nbsp; "
            f"{d['positions']} positions &nbsp;|&nbsp; total "
            f"<b>{self._money(d['total_value'])}</b>")
        rows = d['holdings'][:self.MAX_ROWS]
        self._table.setRowCount(len(rows))
        for r, h in enumerate(rows):
            self._cell(r, 0, str(r + 1), left=True)
            self._cell(r, 1, h['issuer'], left=True)
            self._cell(r, 2, h['class'], left=True)
            self._cell(r, 3, self._money(h['value']))
            self._cell(r, 4, f"{h['pct']:.1f}%")
            self._cell(r, 5, f"{h['shares']:,}")
        extra = '' if d['positions'] <= self.MAX_ROWS else f' (showing top {self.MAX_ROWS})'
        self._status.setText(f"Loaded {len(rows)} positions{extra}.")
        self._draw_pie(d['holdings'])

    def _draw_pie(self, holdings, top_n: int = 10):
        if self._pie_canvas:
            self._pie_layout.removeWidget(self._pie_canvas)
            self._pie_canvas.deleteLater()
            self._pie_canvas = None
        if not holdings:
            return

        top = holdings[:top_n]
        labels = [h['issuer'][:16] for h in top]
        values = [h['value'] for h in top]
        others = sum(h['value'] for h in holdings[top_n:])
        if others > 0:
            labels.append('Others')
            values.append(others)

        fig = Figure(figsize=(3.1, 3.6), facecolor=DARK_BG)
        ax = fig.add_subplot(111)
        ax.pie(values, labels=labels, startangle=90, labeldistance=1.05,
               textprops={'color': TEXT, 'fontsize': 7},
               wedgeprops={'edgecolor': DARK_BG, 'linewidth': 0.8})
        ax.set_title(f'Top {len(top)} by value', color=TEXT, fontsize=10)
        ax.axis('equal')
        fig.tight_layout()

        self._pie_canvas = FigureCanvas(fig)
        self._pie_layout.addWidget(self._pie_canvas)
        self._pie_canvas.draw()


# ── firm market-outlook dialog ────────────────────────────────────────────────

class StrategyPage(QWidget):
    FIRMS = [
        'BlackRock', 'JPMorgan Asset Management', 'Goldman Sachs',
        'Morgan Stanley', 'Vanguard', 'Fidelity', 'UBS',
        'Bank of America', 'Bridgewater Associates',
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._job = None
        self._build_ui()

    def on_show(self):
        pass            # nothing to fetch until the user picks a firm

    def _build_ui(self):
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        self._combo = QComboBox()
        self._combo.setEditable(True)            # allow typing any firm
        for f in self.FIRMS:
            self._combo.addItem(f)
        self._go = QPushButton('Get outlook')
        self._go.setObjectName('AccentBtn')
        self._go.clicked.connect(self._ask)
        row.addWidget(self._combo, 1)
        row.addWidget(self._go)
        layout.addLayout(row)

        self._answer = QTextEdit()
        self._answer.setReadOnly(True)
        self._answer.setPlaceholderText(
            "Pick a firm (or type one) and click Get outlook. Claude searches the "
            "web for their latest published market view and summarizes it.")
        layout.addWidget(self._answer)

        self._status = QLabel('')
        self._status.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        layout.addWidget(self._status)
        disc = QLabel('⚠ Summarizes the firm\'s publicly stated views via live web '
                      'search — not financial advice, and may be incomplete or out '
                      'of date. Verify against the firm\'s official publications.')
        disc.setWordWrap(True)
        disc.setStyleSheet('color: #c9a227; font-size: 11px;')
        layout.addWidget(disc)

    def _ask(self):
        if self._job and self._job.isRunning():
            return
        firm = self._combo.currentText().strip()
        if not firm:
            return
        self._answer.clear()
        self._status.setText(f'Researching {firm} …')
        self._set_busy(True)
        self._job = StrategyWorker(firm)
        self._job.chunk.connect(self._on_chunk)
        self._job.done.connect(self._on_done)
        self._job.error.connect(self._on_error)
        self._job.start()

    def _on_chunk(self, text: str):
        self._answer.moveCursor(self._answer.textCursor().MoveOperation.End)
        self._answer.insertPlainText(text)

    def _on_done(self):
        self._set_busy(False)
        self._status.setText('Done.')

    def _on_error(self, msg: str):
        self._set_busy(False)
        low = msg.lower()
        if 'api_key' in low or 'authentication' in low or 'no claude api key' in low:
            friendly = ('No Claude API key found. Put a .env file with '
                        'ANTHROPIC_API_KEY=... next to the app, then reopen.')
        elif 'connection' in low or 'timeout' in low:
            friendly = ('Could not reach Claude after retries. Check your internet '
                        'and try again — web searches can take a bit longer.')
        elif 'rate' in low or 'overload' in low or '429' in low or '529' in low:
            friendly = 'Claude is busy or rate-limited. Wait a few seconds and retry.'
        else:
            friendly = 'Something went wrong talking to Claude.'
        self._status.setText('Error.')
        if self._answer.toPlainText().strip():
            self._answer.append('\n\n— ' + friendly)
        else:
            self._answer.setPlainText(f'{friendly}\n\nDetails: {msg}')

    def _set_busy(self, busy: bool):
        self._go.setEnabled(not busy)
        self._combo.setEnabled(not busy)


# ── IPO calendar page ─────────────────────────────────────────────────────────

class IPOPage(QWidget):
    HEADERS = ['Symbol', 'Company', 'Exchange', 'Price', 'Shares', 'Date', '$ Value']
    SECTIONS = [('Upcoming', 'upcoming'), ('Priced', 'priced'),
                ('Filed (SEC)', 'filed'), ('Rumored (AI)', 'rumored')]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = {}
        self._job = None
        self._ai_job = None
        self._loaded = False
        self._build_ui()

    def on_show(self):
        if not self._loaded:
            self._loaded = True
            self._load()

    @staticmethod
    def _months(n_back: int = 2, n_fwd: int = 1) -> list:
        from datetime import date
        today = date.today()
        out = []
        for off in range(-n_back, n_fwd + 1):
            y = today.year + (today.month - 1 + off) // 12
            m = (today.month - 1 + off) % 12 + 1
            out.append(f'{y:04d}-{m:02d}')
        return out

    def _build_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self._section = QComboBox()
        for label, _key in self.SECTIONS:
            self._section.addItem(label)
        self._section.currentIndexChanged.connect(self._on_section)
        self._month = QComboBox()
        months = self._months()
        self._month.addItems(months)
        self._month.setCurrentText(months[2] if len(months) > 2 else months[-1])  # current month
        self._month.currentIndexChanged.connect(self._load)
        load = QPushButton('Load')
        load.setObjectName('AccentBtn')
        load.clicked.connect(self._load)
        top.addWidget(QLabel('Show:'))
        top.addWidget(self._section)
        top.addWidget(QLabel('Month:'))
        top.addWidget(self._month)
        top.addWidget(load)
        top.addStretch()
        layout.addLayout(top)

        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)   # Company stretches
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

        # AI text view for the "Rumored" section (hidden unless selected)
        self._ai = QTextEdit()
        self._ai.setReadOnly(True)
        self._ai.setVisible(False)
        self._ai.setPlaceholderText('Rumored / expected IPOs from recent news '
                                    '(AI + live web search)…')
        layout.addWidget(self._ai)

        self._status = QLabel('')
        self._status.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        layout.addWidget(self._status)
        disc = QLabel('Source: Nasdaq IPO calendar (unofficial) for Upcoming/Priced/Filed '
                      '— companies that have actually filed with the SEC. "Rumored (AI)" '
                      'is press speculation via web search — unconfirmed, not a filing. '
                      'For information only.')
        disc.setWordWrap(True)
        disc.setStyleSheet(f'color: {SUBTEXT}; font-size: 10px;')
        layout.addWidget(disc)

    def _section_key(self):
        return self.SECTIONS[self._section.currentIndex()][1]

    def _on_section(self):
        if self._section_key() == 'rumored':
            self._table.setVisible(False)
            self._ai.setVisible(True)
            if not self._ai.toPlainText().strip() and not (
                    self._ai_job and self._ai_job.isRunning()):
                self._gen_rumored()
        else:
            self._ai.setVisible(False)
            self._table.setVisible(True)
            self._render()

    def _load(self):
        if self._section_key() == 'rumored':
            self._gen_rumored()
            return
        if self._job and self._job.isRunning():
            return
        month = self._month.currentText()
        self._status.setText(f'Fetching IPO calendar for {month} …')
        self._job = IPOWorker(month)
        self._job.ready.connect(self._on_ready)
        self._job.error.connect(lambda m: self._status.setText('Error: ' + m))
        self._job.start()

    def _gen_rumored(self):
        if self._ai_job and self._ai_job.isRunning():
            return
        self._ai.clear()
        self._status.setText('Searching the news for rumored / expected IPOs …')
        self._ai_job = RumoredIPOWorker()
        self._ai_job.chunk.connect(self._on_ai_chunk)
        self._ai_job.done.connect(lambda: self._status.setText('Done — rumored / unconfirmed.'))
        self._ai_job.error.connect(lambda m: self._status.setText('Error: ' + m))
        self._ai_job.start()

    def _on_ai_chunk(self, text: str):
        self._ai.moveCursor(self._ai.textCursor().MoveOperation.End)
        self._ai.insertPlainText(text)

    def _on_ready(self, data: dict):
        self._data = data
        self._render()

    def _render(self):
        key = self.SECTIONS[self._section.currentIndex()][1]
        rows = self._data.get(key, [])
        self._table.setRowCount(len(rows))
        for r, item in enumerate(rows):
            cells = [item['symbol'], item['company'], item['exchange'],
                     item['price'], item['shares'], item['date'], item['amount']]
            for c, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | (
                    Qt.AlignmentFlag.AlignLeft if c in (1, 2) else (
                        Qt.AlignmentFlag.AlignLeft if c == 0 else Qt.AlignmentFlag.AlignRight)))
                self._table.setItem(r, c, cell)
        label = self.SECTIONS[self._section.currentIndex()][0]
        self._status.setText(f'{len(rows)} {label.lower()} IPO(s) for '
                             f'{self._month.currentText()}.')


# ── stock analysis page (targets + levels) ────────────────────────────────────

class StockAnalysisPage(QWidget):
    HEADERS = ['Reference Level', 'Price', 'vs Current', 'Type']

    def __init__(self, parent=None, main=None):
        super().__init__(parent)
        self._main = main
        self._job = None
        self._loaded_symbol = None
        self._build_ui()

    def on_show(self):
        sym = (self._main._selected if self._main else None)
        if sym and sym != self._loaded_symbol:
            self._sym.setText(sym)
            self._load()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self._sym = QLineEdit()
        self._sym.setPlaceholderText('Symbol (e.g. AAPL)')
        self._sym.setFixedWidth(140)
        self._sym.returnPressed.connect(self._load)
        load = QPushButton('Load')
        load.setObjectName('AccentBtn')
        load.clicked.connect(self._load)
        top.addWidget(QLabel('Stock:'))
        top.addWidget(self._sym)
        top.addWidget(load)
        top.addStretch()
        layout.addLayout(top)

        hdr = QLabel('Analyst Price Target')
        hdr.setFont(QFont('Segoe UI', 12, QFont.Weight.Bold))
        layout.addWidget(hdr)
        self._target = QLabel('Select a stock (or click one in the watchlist).')
        self._target.setTextFormat(Qt.TextFormat.RichText)
        self._target.setWordWrap(True)
        layout.addWidget(self._target)

        hdr2 = QLabel('Technical Reference Levels')
        hdr2.setFont(QFont('Segoe UI', 12, QFont.Weight.Bold))
        layout.addWidget(hdr2)
        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

        self._status = QLabel('')
        self._status.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px;')
        layout.addWidget(self._status)
        disc = QLabel('Analyst targets via Yahoo Finance (consensus of analyst estimates). '
                      'Technical levels are computed reference points (support/resistance, '
                      'moving averages, Bollinger bands) — NOT buy/sell advice. Levels above '
                      'the price are resistance; below are support.')
        disc.setWordWrap(True)
        disc.setStyleSheet('color: #c9a227; font-size: 11px;')
        layout.addWidget(disc)

    def _load(self):
        sym = self._sym.text().strip().upper()
        if not sym:
            return
        if self._job and self._job.isRunning():
            return
        self._status.setText(f'Loading analysis for {sym} …')
        self._job = AnalysisWorker(sym)
        self._job.ready.connect(self._on_ready)
        self._job.error.connect(lambda m: self._status.setText('Error: ' + m))
        self._job.start()

    @staticmethod
    def _money(v):
        return f'${v:,.2f}' if v else 'n/a'

    def _on_ready(self, d: dict):
        self._loaded_symbol = d['symbol']
        t = d['target']
        up = t.get('upside_pct')
        upcol = UP_COLOR if (up or 0) >= 0 else DOWN_COLOR
        upstr = (f" <span style='color:{upcol}'>({up:+.1f}% "
                 f"{'upside' if up >= 0 else 'downside'})</span>") if up is not None else ''
        self._target.setText(
            f"<b>{d['symbol']}</b> &nbsp; current {self._money(t['current'])}<br>"
            f"Analyst mean target: <b>{self._money(t['mean'])}</b>{upstr} &nbsp;|&nbsp; "
            f"{t.get('n_analysts') or 0} analysts &nbsp;|&nbsp; rec: "
            f"<b>{t.get('rec_key') or 'n/a'}</b><br>"
            f"High {self._money(t['high'])} &nbsp; Median {self._money(t['median'])} "
            f"&nbsp; Low {self._money(t['low'])}")

        cur = d['levels'].get('current')
        levels = d['levels'].get('levels', [])
        self._table.setRowCount(len(levels))
        for r, lv in enumerate(levels):
            above = cur is not None and lv['value'] >= cur
            color = DOWN_COLOR if above else UP_COLOR
            tag = 'resistance' if above else 'support'
            cells = [lv['name'], f"${lv['value']:,.2f}", f"{lv['pct']:+.1f}%", tag]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | (
                    Qt.AlignmentFlag.AlignLeft if c == 0 else Qt.AlignmentFlag.AlignRight))
                if c >= 1:
                    item.setForeground(QColor(color))
                self._table.setItem(r, c, item)
        self._status.setText(f"Loaded {d['symbol']} — {len(levels)} reference levels.")


# ── main window ──────────────────────────────────────────────────────────────

