# Stock Monitor

A desktop stock-monitoring prototype (PyQt6) with a SCADA-style dark UI:
persistent watchlist, candlestick charts with EMA overlays and a crosshair
inspector, portfolio P&L tracking, and Claude-powered, portfolio-aware analysis.

![chart](smoke_test.png)

## Features

- **Watchlist** — add stocks by company name (autocomplete via Yahoo search) or
  ticker; persisted to `watchlist.json` across restarts. Each row shows the latest
  close and % change, colored green (up) / red (down). Auto-refreshes every 60s.
- **Candlestick charts** — selectable time ranges (1D / 5D / 1M / 3M / 6M / 1Y /
  2Y / 5Y), volume sub-panel, and **5 / 10 / 20 EMA** overlays.
- **Crosshair inspector** — press-and-hold on the chart to see the date and OHLC +
  the price under your cursor.
- **Portfolio / P&L** — import holdings + cash from CSV or add them via a form;
  see per-position and total unrealized P&L. Persisted to `portfolio.json`.
- **Transactions / Ledger** — record buys, sells, and dividends. Open positions
  sync to the Portfolio page; realized P&L (average-cost) and dividends are
  tracked, and the History chart reconstructs accurately from actual trades.
- **Portfolio History** — reconstructs your portfolio's value curve from your
  transaction ledger (or purchase dates) + historical prices ("from start to
  now"), and records daily snapshots going forward. Shows value vs. cost basis.
- **Market Sentiment** — CNN Fear & Greed (+ its 7 components), the crypto
  Fear & Greed index, and the VIX, on color-coded gauges.
- **13F Holdings** — browse what famous institutional managers (Berkshire,
  ARK/Cathie Wood, BlackRock, etc.) hold, from their latest SEC EDGAR 13F filing.
- **Firm Outlook** — AI-summarized current market view/strategy for major firms
  (BlackRock, JPMorgan, Goldman, etc.) via live web search, with sources.
- **IPO Calendar** — upcoming, priced, and SEC-filed IPOs by month (Nasdaq feed).
- **AI Insights** — sends your portfolio + watchlist snapshot to Claude for
  portfolio-aware analysis with **live web search** (current news, fundamentals,
  volatility), citing sources.

## Requirements

- Python 3.10+
- A [Claude API key](https://console.anthropic.com/) for the AI Insights feature
  (everything else works without it).

## Setup

```bash
pip install -r requirements.txt
```

### Claude API key (only needed for AI Insights)

**Easiest — use a `.env` file (recommended):** copy `.env.example` to `.env` in
this folder and put your key in it. The app loads it automatically on startup, so
it works no matter how you launch the app. `.env` is gitignored.

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

> No quotes, no spaces around the `=`.

**Alternative — a real environment variable.** Note that `set ANTHROPIC_API_KEY=...`
in cmd only applies to *that one window* and won't reach the app if you launch it
elsewhere. To make it stick:

```powershell
# Windows — permanent (then open a NEW terminal so it takes effect)
setx ANTHROPIC_API_KEY "sk-ant-..."

# Windows — current session only; you must run `python main.py` in the SAME window
set ANTHROPIC_API_KEY=sk-ant-...
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY="sk-ant-..."
```

A real environment variable takes precedence over the `.env` file.

## Run

```bash
python main.py
```

On Windows you can also double-click `run.bat`.

## Build a standalone .exe (Windows)

To produce a double-clickable executable that runs without Python installed:

```bash
pip install pyinstaller
pyinstaller --noconfirm StockMonitor.spec
```

Or double-click `build_exe.bat`. The result is `dist\StockMonitor.exe` (~90 MB,
single file). Put your `.env` file **next to the .exe** for AI Insights; the app
reads/writes `watchlist.json`, `portfolio.json`, and `alerts.json` in the same
folder as the executable.

## Portfolio CSV format

Columns: `symbol, shares, avg_cost`. A row with symbol `CASH` sets the cash
balance (its number goes in the `shares` column). A header row is optional.

```csv
symbol,shares,avg_cost
AAPL,20,180.50
MSFT,10,400.00
CASH,5000,
```

See `sample_portfolio.csv` for a ready-made example.

## Project layout

| File | Purpose |
|---|---|
| `main.py` | App shell: MainWindow, navigation, entry point |
| `theme.py` | Display constants (time ranges, EMA spans, palette) |
| `appstate.py` | Model layer (no Qt): portfolio P&L, alerts, file paths |
| `workers.py` | Background QThread workers (network off the UI thread) |
| `widgets.py` | Watchlist row + candlestick chart panel |
| `dialogs.py` | Feature pages (embedded) + small pop-up dialogs |
| `data.py` | Cached market-data layer (yfinance + TTL cache + retry) |
| `indicators.py` | RSI / MACD / Bollinger / SMA math |
| `sentiment.py` | Fear & Greed + VIX sentiment data |
| `ledger.py` | Transaction ledger → positions, realized P&L, dividends |
| `history.py` | Portfolio value reconstruction + daily snapshots |
| `thirteenf.py` | SEC EDGAR 13F holdings fetch + parse |
| `ipo.py` | Nasdaq IPO calendar fetch + parse |
| `llm.py` | Claude prompt builders + streaming analysis (with web search) |
| `test_functions.py` | Headless unit tests (persistence, EMA, P&L, prompts, cache) |
| `smoke_test.py` | Launches the GUI, loads a chart, screenshots it |
| `portfolio_smoke.py` | Renders the Portfolio dialog offline and screenshots it |

## Testing

```bash
python test_functions.py     # unit tests (some require internet)
python smoke_test.py         # GUI smoke test -> smoke_test.png
python portfolio_smoke.py    # portfolio dialog -> portfolio_smoke.png
```

## Notes & limitations

- Market data is from Yahoo Finance (`yfinance`) — free but no SLA; it can be
  delayed or rate-limited. The cache layer in `data.py` softens this.
- P&L is **unrealized only** (no dividends, fees, or realized gains), uses average
  cost basis, and does not normalize currencies across exchanges.
- AI Insights is educational analysis, **not financial advice**. The model only
  sees the prices shown in the app — it has no independent live market data.
