"""Display constants: time ranges, EMA spans, and the dark palette."""

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
