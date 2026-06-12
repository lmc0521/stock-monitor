"""Stock Monitor — application shell (MainWindow) and entry point."""

import sys
import os
import json

import matplotlib
matplotlib.use('QtAgg')

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QListWidgetItem, QLineEdit, QLabel,
    QMessageBox, QSizePolicy, QFrame, QCompleter, QDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QFileDialog, QTextEdit,
    QFormLayout, QInputDialog, QComboBox, QProgressBar, QScrollArea, QStackedWidget
)
from PyQt6.QtCore import Qt, QTimer, QStringListModel
from PyQt6.QtGui import QFont

import data
from theme import (PERIODS, EMAS, DARK_BG, PANEL_BG, ACCENT, HIGHLIGHT, TEXT,
                   SUBTEXT, UP_COLOR, DOWN_COLOR)
from appstate import (_app_dir, _APP_DIR, WATCHLIST_FILE, PORTFOLIO_FILE, ALERTS_FILE,
                      parse_portfolio_csv, compute_portfolio, load_portfolio,
                      save_portfolio, load_alerts, save_alerts, evaluate_alerts)
from workers import (DataFetcher, QuoteFetcher, SearchWorker, PriceFetcher,
                     SentimentWorker, HistoryWorker, ThirteenFWorker, InsightsWorker,
                     StrategyWorker, SnapshotWorker)
from widgets import StockRow, ChartPanel
from dialogs import (AddHoldingDialog, AlertsDialog, PortfolioPage, InsightsPage,
                     SentimentPage, LedgerPage, HistoryPage, ThirteenFPage, StrategyPage,
                     IPOPage, StockAnalysisPage)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Stock Monitor')
        self.setMinimumSize(1320, 760)

        self._watchlist  = self._load_watchlist()
        self._fetcher    = None
        self._quote_job  = None
        self._search_job = None
        self._add_job    = None
        self._selected   = None
        self._period_key = '1M'
        self._indicators = set()       # active overlays: BBANDS / RSI / MACD
        self._rows       = {}          # symbol -> StockRow
        self._sugg_map   = {}          # display string -> symbol
        self._alerts     = load_alerts()

        self._build_ui()
        self._apply_theme()
        self._nav_btns['chart'].setChecked(True)   # start on the chart page
        self._refresh_list()
        self._refresh_quotes()

        # auto-refresh quotes every 60s (cached layer keeps this cheap)
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(60_000)
        self._auto_timer.timeout.connect(self._refresh_quotes)
        self._auto_timer.start()

        # record today's portfolio snapshot shortly after launch (background),
        # so the value history stays complete without opening the Portfolio page
        self._snapshot_job = None
        QTimer.singleShot(5_000, self._startup_snapshot)

    def _startup_snapshot(self):
        if self._snapshot_job and self._snapshot_job.isRunning():
            return
        self._snapshot_job = SnapshotWorker()
        self._snapshot_job.done.connect(
            lambda v: self._status.setText(
                self._status.text() + f'   |   📸 snapshot recorded ({v:,.0f})'))
        self._snapshot_job.start()

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
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        outer.addWidget(self._build_nav_bar(), 0)         # navigation across the top
        body = QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(self._build_left_panel(), 0)       # watchlist gets the full left column
        body.addWidget(self._build_right_panel(), 1)
        outer.addLayout(body, 1)

    def _build_nav_bar(self) -> QWidget:
        """Top navigation bar — switches the embedded page on the right."""
        bar = QFrame()
        bar.setObjectName('NavBar')
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 5, 6, 5)
        h.setSpacing(4)

        nav = [
            ('📈 Chart',     'chart'),
            ('🎯 Analysis',  'analysis'),
            ('📊 Portfolio', 'portfolio'),
            ('🧾 Ledger',    'ledger'),
            ('🕒 History',   'history'),
            ('💡 Insights',  'insights'),
            ('😱 Sentiment', 'sentiment'),
            ('🏦 13F',       'f13'),
            ('🏛 Outlook',   'strategy'),
            ('🚀 IPO',       'ipo'),
        ]
        self._nav_btns = {}
        for label, key in nav:
            btn = QPushButton(label)
            btn.setObjectName('NavBtn')
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, k=key: self._show_page(k))
            h.addWidget(btn)
            self._nav_btns[key] = btn

        h.addStretch()

        # data-health indicator: green when all sources OK, red names when not
        self._health_lbl = QLabel('● data')
        self._health_lbl.setStyleSheet(f'color: {SUBTEXT}; font-size: 11px; padding: 0 6px;')
        h.addWidget(self._health_lbl)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(10_000)
        self._health_timer.timeout.connect(self._update_health)
        self._health_timer.start()

        alert_btn = QPushButton('🔔 Alerts')
        alert_btn.setObjectName('AccentBtn')
        alert_btn.clicked.connect(self._open_alerts)
        h.addWidget(alert_btn)
        return bar

    def _update_health(self):
        health = data.get_health()
        if not health:
            return
        bad = [k for k, v in health.items() if not v.get('ok')]
        if bad:
            self._health_lbl.setText('● ' + ', '.join(bad) + ' down')
            self._health_lbl.setStyleSheet(
                f'color: {DOWN_COLOR}; font-size: 11px; padding: 0 6px;')
        else:
            self._health_lbl.setText('● data OK')
            self._health_lbl.setStyleSheet(
                f'color: {UP_COLOR}; font-size: 11px; padding: 0 6px;')
        tip = '\n'.join(
            f"{k}: {'OK' if v.get('ok') else 'FAILED — ' + v.get('error', '')}"
            for k, v in sorted(health.items()))
        self._health_lbl.setToolTip(tip)

    def closeEvent(self, event):
        data.save_cache(force=True)        # persist the data cache across restarts
        super().closeEvent(event)

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
        layout.addWidget(self._list, 1)                   # fills the rest of the column

        btn_row = QHBoxLayout()
        rm_btn = QPushButton('Remove')
        rm_btn.clicked.connect(self._remove_stock)
        ref_btn = QPushButton('↻ Refresh')
        ref_btn.clicked.connect(self._hard_refresh)
        btn_row.addWidget(rm_btn)
        btn_row.addWidget(ref_btn)
        layout.addLayout(btn_row)

        return panel

    def _build_right_panel(self) -> QWidget:
        self._stack = QStackedWidget()

        # page 0: the chart view
        chart_page = QWidget()
        cl = QVBoxLayout(chart_page)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        cl.addLayout(self._build_period_bar())
        self._chart = ChartPanel()
        cl.addWidget(self._chart)
        self._status = QLabel('Ready — add a stock and click it to load a chart.')
        self._status.setObjectName('StatusBar')
        cl.addWidget(self._status)

        # the embedded feature pages (formerly pop-up dialogs)
        self._pages = {
            'chart':     chart_page,
            'analysis':  StockAnalysisPage(main=self),
            'portfolio': PortfolioPage(),
            'ledger':    LedgerPage(),
            'history':   HistoryPage(),
            'insights':  InsightsPage(main=self),
            'sentiment': SentimentPage(),
            'f13':       ThirteenFPage(),
            'strategy':  StrategyPage(),
            'ipo':       IPOPage(),
        }
        for key in ('chart', 'analysis', 'portfolio', 'ledger', 'history',
                    'insights', 'sentiment', 'f13', 'strategy', 'ipo'):
            self._stack.addWidget(self._pages[key])
        return self._stack

    def _show_page(self, key: str):
        page = self._pages.get(key)
        if page is None:
            return
        self._stack.setCurrentWidget(page)
        for k, btn in self._nav_btns.items():
            btn.setChecked(k == key)
        if hasattr(page, 'on_show'):
            page.on_show()

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
        if self._add_job and self._add_job.isRunning():
            return
        # validate the symbol has market data before adding (catches typos)
        self._status.setText(f'Checking {sym} …')
        self._add_job = PriceFetcher([sym])
        self._add_job.prices_ready.connect(lambda prices, s=sym: self._on_add_validated(s, prices))
        self._add_job.start()

    def _on_add_validated(self, sym: str, prices: dict):
        if sym not in prices:
            self._status.setText('')
            QMessageBox.warning(
                self, 'Symbol not found',
                f"Couldn't find market data for \"{sym}\".\n\n"
                "Check the spelling, or start typing the company name and pick "
                "a suggestion from the list.")
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
        self._check_alerts(sym, price, pct)

    # ── price alerts ──
    def _open_alerts(self):
        AlertsDialog(self, self._alerts, list(self._watchlist),
                     on_change=self._sync_alarms).exec()
        self._sync_alarms()

    def _check_alerts(self, sym: str, price: float, pct: float | None = None):
        fired = evaluate_alerts(self._alerts, sym, price, pct)
        if not fired:
            return
        save_alerts(self._alerts)
        QApplication.beep()
        lines = [
            (f"{a['symbol']} moved {pct:+.2f}% today (threshold ±{a['price']:g}%)"
             if a.get('condition') == 'move' else
             f"{a['symbol']} went {a['condition']} {a['price']:.2f}  (now {price:.2f})")
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

    # ── chart ──
    def _on_stock_clicked(self, item):
        self._selected = item.data(Qt.ItemDataRole.UserRole)
        self._show_page('chart')
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
            QFrame#NavBar {{
                background-color: {PANEL_BG}; border-radius: 8px;
                border: 1px solid #2a2a4a;
            }}
            QPushButton#NavBtn {{
                background-color: {ACCENT}; border: 1px solid #2a2a4a;
                border-radius: 6px; padding: 7px 6px; font-size: 12px;
            }}
            QPushButton#NavBtn:hover {{ background-color: #1a4a80; }}
            QPushButton#NavBtn:checked {{
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
