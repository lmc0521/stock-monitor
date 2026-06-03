"""
GUI smoke test for Stock Monitor.

Launches the real MainWindow, programmatically:
  - adds a stock (AAPL) to the watchlist
  - selects it so the candlestick + EMA chart renders
  - waits for the async chart fetch AND the async quote fetch to finish
  - saves a screenshot of the whole window to  smoke_test.png
  - exits automatically

This lets you eyeball the EMA lines and the coloured price row without
clicking anything yourself.

Run:
    python smoke_test.py
"""

import os
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer, Qt

import main

SYMBOL   = 'AAPL'
OUT_PNG  = os.path.join(os.path.dirname(__file__), 'smoke_test.png')
TIMEOUT_MS = 30000        # hard stop so it never hangs


def run():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    win = main.MainWindow()
    win.show()

    state = {'added': False, 'done': False, 'elapsed': 0}

    def step():
        # 1) add + select the symbol once the window is up
        if not state['added']:
            win._input.setText(SYMBOL)
            win._add_stock()
            if win._list.count() > 0:
                item = win._list.item(0)
                win._list.setCurrentItem(item)
                win._on_stock_clicked(item)   # kicks off async chart fetch
            state['added'] = True
            return

        # 2) wait until the chart canvas exists AND the row has a live quote
        row = win._rows.get(SYMBOL)
        chart_ready = win._chart._canvas is not None
        quote_ready = row is not None and '%' in row._price.text()

        state['elapsed'] += 200
        if (chart_ready and quote_ready) or state['elapsed'] >= TIMEOUT_MS:
            timer.stop()
            # let the matplotlib canvas actually paint before we grab the window
            QTimer.singleShot(800, lambda: finish(win, app, chart_ready, quote_ready))

    def finish(win, app, chart_ready, quote_ready):
        if state['done']:
            return
        state['done'] = True

        # simulate a long-press in the middle of the chart to trigger the crosshair
        crosshair_ok = False
        chart = win._chart
        if chart._canvas is not None and chart._ax is not None:
            from matplotlib.backend_bases import MouseEvent
            ax = chart._ax
            # a point roughly mid-chart, mid-price (in data coords -> display coords)
            xmid = len(chart._df) * 0.6
            ymid = (chart._df['Low'].min() + chart._df['High'].max()) / 2
            px, py = ax.transData.transform((xmid, ymid))
            evt = MouseEvent('button_press_event', chart._canvas, px, py, button=1)
            evt.inaxes, evt.xdata, evt.ydata = ax, xmid, ymid
            chart._on_press(evt)
            chart._activate()                 # skip the long-press wait
            crosshair_ok = chart._annot.get_visible()

        # force a repaint of the chart canvas, then flush the event queue
        if chart._canvas is not None:
            chart._canvas.draw()
            chart._canvas.flush_events()
        app.processEvents()

        pixmap = win.grab()
        ok = pixmap.save(OUT_PNG, 'PNG')
        state['crosshair_ok'] = crosshair_ok

        print('=' * 56)
        print('Stock Monitor — GUI smoke test')
        print('=' * 56)
        print(f'  chart rendered ............ {"YES" if chart_ready else "NO"}')
        print(f'  crosshair tooltip shown ... {"YES" if state.get("crosshair_ok") else "NO"}')
        print(f'  live quote populated ...... {"YES" if quote_ready else "NO"}')
        if quote_ready:
            print(f'  row text .................. {win._rows[SYMBOL]._price.text()!r}')
        print(f'  screenshot saved .......... {"YES" if ok else "NO"}  -> {OUT_PNG}')
        print('=' * 56)

        exit_code = 0 if (chart_ready and ok) else 1
        QTimer.singleShot(100, lambda: app.exit(exit_code))

    timer = QTimer()
    timer.timeout.connect(step)
    timer.start(200)

    sys.exit(app.exec())


if __name__ == '__main__':
    run()
