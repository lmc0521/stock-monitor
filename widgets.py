"""Reusable widgets: the watchlist row and the candlestick chart panel."""

import matplotlib
matplotlib.use('QtAgg')
from matplotlib.lines import Line2D
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
import mplfinance as mpf

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

import indicators as ta
from theme import (EMAS, DARK_BG, PANEL_BG, ACCENT, HIGHLIGHT, TEXT, SUBTEXT,
                   UP_COLOR, DOWN_COLOR)

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
