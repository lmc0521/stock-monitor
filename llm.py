"""
Claude-powered, portfolio-aware investment analysis.

Kept separate from the Qt UI so the prompt-building logic is pure and unit-testable
without a network connection or API key. The only network-touching function is
`stream_insights`, which yields text chunks from a streaming Claude response.

Requires the ANTHROPIC_API_KEY environment variable.
"""

from __future__ import annotations

# Opus 4.8 — most capable model; adaptive thinking only (no budget_tokens / sampling params).
MODEL = "claude-opus-4-8"
MAX_TOKENS = 8192

# Stable system prompt — kept byte-frozen so it can be prompt-cached across requests.
SYSTEM_PROMPT = """You are a portfolio-aware investment analysis assistant embedded in a \
desktop stock-monitoring app. The user will give you a snapshot of their current holdings \
(with cost basis and unrealized P&L), their cash balance, and a watchlist of symbols they \
are tracking, followed by a question.

Your job:
- Ground every observation in the specific numbers you are given. Reference actual symbols, \
weights, and P&L figures rather than speaking generically.
- Analyse concentration and diversification: call out positions that dominate the portfolio \
by market value, and sectors/themes that are over- or under-represented if you can infer them.
- Surface risks the user may not have noticed (single-name concentration, correlated names, \
large unrealized losses, oversized cash drag, currency mix when tickers span exchanges).
- When asked for ideas, explain the *reasoning* behind each suggestion and tie it to the \
user's existing exposure. Offer a confidence level and the main risk for each idea.
- Be concise and skimmable. Use short markdown sections and bullet points. Lead with the \
single most important takeaway.

Tools — you can access the internet:
- You have web_search and web_fetch tools. Use them to look up current fundamentals, \
valuations, recent news, analyst commentary, earnings dates, and volatility for the user's \
symbols whenever it would sharpen the analysis. Prefer recent, reputable sources and cite \
them inline with the source name and date (e.g. "(Reuters, 2026-06-02)").
- Treat the prices in the user's message as their authoritative portfolio snapshot for cost \
basis and P&L. Use the web to add context around them (recent moves, catalysts, whether a \
name is near its highs or lows), not to second-guess the user's stated holdings.

Hard rules:
- Do not fabricate data. If a search returns nothing useful or you are unsure, say so plainly \
rather than guessing, and distinguish the user's snapshot numbers from what you found online.
- This is analysis and education, NOT personalized financial advice. Do not tell the user to \
buy or sell. Frame ideas as considerations to research, and end every response with a one-line \
reminder that this is not financial advice and that they should do their own research."""


def build_portfolio_context(portfolio_result: dict, watchlist_quotes: dict | None = None) -> str:
    """
    Turn a compute_portfolio() result (+ optional watchlist prices) into a compact,
    deterministic text block to feed the model. Pure function — no I/O.
    """
    lines: list[str] = []
    rows = portfolio_result.get("rows", [])

    lines.append("## Current holdings")
    if rows:
        lines.append("symbol | shares | avg_cost | price | mkt_value | cost | pnl | pnl_%")
        for r in rows:
            price = "n/a" if r["price"] is None else f"{r['price']:.2f}"
            mkt   = "n/a" if r["mkt"]   is None else f"{r['mkt']:.2f}"
            pnl   = "n/a" if r["pnl"]   is None else f"{r['pnl']:+.2f}"
            pct   = "n/a" if r["pnl_pct"] is None else f"{r['pnl_pct']:+.2f}%"
            lines.append(
                f"{r['symbol']} | {r['shares']:g} | {r['avg_cost']:.2f} | "
                f"{price} | {mkt} | {r['cost']:.2f} | {pnl} | {pct}"
            )
    else:
        lines.append("(no holdings)")

    lines.append("")
    lines.append("## Totals")
    lines.append(f"invested_cost: {portfolio_result.get('invested_cost', 0):.2f}")
    lines.append(f"cash: {portfolio_result.get('cash', 0):.2f}")
    lines.append(f"market_value: {portfolio_result.get('market_value', 0):.2f}")
    lines.append(f"total_value: {portfolio_result.get('total_value', 0):.2f}")
    lines.append(
        f"total_pnl: {portfolio_result.get('total_pnl', 0):+.2f} "
        f"({portfolio_result.get('total_pnl_pct', 0):+.2f}%)"
    )

    if watchlist_quotes:
        lines.append("")
        lines.append("## Watchlist (tracked, not held)")
        for sym in sorted(watchlist_quotes):
            price = watchlist_quotes[sym]
            lines.append(f"{sym}: {price:.2f}" if price is not None else f"{sym}: n/a")

    return "\n".join(lines)


def build_user_prompt(portfolio_result: dict, watchlist_quotes: dict | None,
                      question: str) -> str:
    """Assemble the full user-turn prompt: data block + the question. Pure function."""
    context = build_portfolio_context(portfolio_result, watchlist_quotes)
    question = (question or "").strip() or "Give me a portfolio-aware analysis and any ideas worth researching."
    return (
        "Here is my portfolio and watchlist snapshot.\n\n"
        f"{context}\n\n"
        "---\n\n"
        f"My question: {question}"
    )


# ── firm strategy / market outlook ───────────────────────────────────────────

FIRM_SYSTEM_PROMPT = """You are a research assistant that summarizes a named \
investment firm's CURRENT, publicly-stated market outlook and strategy for the \
user. Use the web_search and web_fetch tools to find the firm's most recent house \
view — outlook reports, CIO/strategist commentary, "guide to the markets", or \
similar — ideally from the last few months.

Structure the summary as short markdown sections:
- **Overall stance** — their current risk posture (e.g. risk-on/neutral/defensive).
- **Key calls** — concrete views on equities, bonds/rates, regions, sectors, and \
the US dollar where stated.
- **Themes** — the big ideas they're emphasizing (e.g. AI capex, reshoring, rate cuts).
- **Risks they flag** — what they say could go wrong.

Hard rules:
- These are the FIRM's views, not yours and not the app's. Attribute clearly and \
cite each source inline with name + date (e.g. "(BlackRock Investment Institute, 2026-05)").
- Use the web; do not rely on memory for current positioning. If you can't find \
recent published views, say so plainly rather than inventing them.
- This is a summary for education, NOT financial advice. End with a one-line \
reminder that this summarizes the firm's views and is not advice."""


def build_firm_prompt(firm: str) -> str:
    firm = (firm or "").strip() or "BlackRock"
    return (
        f"Summarize {firm}'s current market outlook and investment strategy. "
        "Search the web for their most recent published house view / outlook "
        "(last few months), and cite the sources with dates."
    )


def _make_client():
    """Build an Anthropic client with explicit auth + generous timeout."""
    import os
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "No Claude API key found. Put ANTHROPIC_API_KEY in a .env file "
            "next to the app, or set it as an environment variable.")
    # a blank ANTHROPIC_AUTH_TOKEN would produce a bad 'Bearer ' header that
    # surfaces as a misleading "connection error"
    if not (os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip():
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    return anthropic.Anthropic(api_key=key, max_retries=3, timeout=120.0)


def _stream(system_text: str, user_prompt: str, *, client=None, attempts: int = 3):
    """
    Shared streaming generator: web-search-enabled, with retry-on-transient-error
    backoff (only before any text is produced, to avoid duplicating output).
    """
    import time
    import anthropic

    client = client or _make_client()

    transient = (anthropic.APIConnectionError, anthropic.InternalServerError)
    for extra in ("RateLimitError", "OverloadedError"):
        cls = getattr(anthropic, extra, None)
        if cls:
            transient = transient + (cls,)

    for attempt in range(attempts):
        produced = False
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                tools=[
                    {"type": "web_search_20260209", "name": "web_search"},
                    {"type": "web_fetch_20260209", "name": "web_fetch"},
                ],
                system=[{
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                for text in stream.text_stream:
                    produced = True
                    yield text
            return
        except transient:
            if produced or attempt == attempts - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def stream_insights(question: str, portfolio_result: dict,
                    watchlist_quotes: dict | None = None, *, client=None,
                    attempts: int = 3):
    """Yield text chunks from a streaming, portfolio-aware, web-search Claude response."""
    user_prompt = build_user_prompt(portfolio_result, watchlist_quotes, question)
    yield from _stream(SYSTEM_PROMPT, user_prompt, client=client, attempts=attempts)


def stream_firm_strategy(firm: str, *, client=None, attempts: int = 3):
    """Yield text chunks summarizing a firm's current market outlook (web-search)."""
    yield from _stream(FIRM_SYSTEM_PROMPT, build_firm_prompt(firm),
                       client=client, attempts=attempts)
