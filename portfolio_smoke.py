"""
GUI smoke test for the Portfolio / P&L dialog.

Builds the real PortfolioDialog, injects holdings + fixed prices (no network),
renders the P&L table, and saves a screenshot to portfolio_smoke.png.

Run:
    python portfolio_smoke.py
"""

import os
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer

import main

OUT_PNG = os.path.join(os.path.dirname(__file__), 'portfolio_smoke.png')

# Fixed holdings + prices so the screenshot is deterministic and offline.
HOLDINGS = [
    {'symbol': 'AAPL', 'shares': 20, 'avg_cost': 180.00},   # cost 3600  -> mkt 6126.20
    {'symbol': 'MSFT', 'shares': 10, 'avg_cost': 400.00},   # cost 4000  -> mkt 4605.20
    {'symbol': 'NVDA', 'shares': 50, 'avg_cost': 120.00},   # cost 6000  -> mkt 11218.0
    {'symbol': 'TSLA', 'shares': 15, 'avg_cost': 450.00},   # cost 6750  -> mkt 6238.20 (loss)
]
PRICES = {'AAPL': 306.31, 'MSFT': 460.52, 'NVDA': 224.36, 'TSLA': 415.88}
CASH = 5000.0


def run():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # build a hidden main window only to obtain the shared dark stylesheet
    win = main.MainWindow.__new__(main.MainWindow)
    from PyQt6.QtWidgets import QMainWindow
    QMainWindow.__init__(win)
    win._apply_theme()

    dlg = main.PortfolioDialog(win)
    dlg.setStyleSheet(win.styleSheet())     # inherit the dark theme
    dlg._holdings = HOLDINGS
    dlg._cash = CASH
    dlg._prices = PRICES
    dlg._render()
    dlg._status.setText('Loaded sample portfolio (offline smoke test).')
    dlg.show()

    def finish():
        result = main.compute_portfolio(HOLDINGS, PRICES, CASH)
        ok = dlg.grab().save(OUT_PNG, 'PNG')
        print('=' * 56)
        print('Portfolio dialog — GUI smoke test')
        print('=' * 56)
        print(f'  rows rendered ............. {dlg._table.rowCount()}')
        print(f'  invested cost ............. {result["invested_cost"]:,.2f}')
        print(f'  market value .............. {result["market_value"]:,.2f}')
        print(f'  total value (incl cash) ... {result["total_value"]:,.2f}')
        print(f'  total P&L ................. {result["total_pnl"]:+,.2f} '
              f'({result["total_pnl_pct"]:+.2f}%)')
        print(f'  screenshot saved .......... {"YES" if ok else "NO"}  -> {OUT_PNG}')
        print('=' * 56)
        QTimer.singleShot(50, app.quit)

    QTimer.singleShot(500, finish)
    app.exec()


if __name__ == '__main__':
    run()
