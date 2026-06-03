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

Hard rules:
- You do NOT have live market data beyond the prices provided in the user's message. Never \
invent or guess current prices, fundamentals, or news. If a judgement needs data you don't \
have, say so explicitly.
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


def stream_insights(question: str, portfolio_result: dict,
                    watchlist_quotes: dict | None = None, *, client=None):
    """
    Yield text chunks from a streaming, portfolio-aware Claude response.

    `client` can be injected for testing; otherwise a default anthropic.Anthropic()
    is created (reads ANTHROPIC_API_KEY from the environment).
    """
    import anthropic  # imported lazily so the rest of the app runs without the package

    client = client or anthropic.Anthropic()
    user_prompt = build_user_prompt(portfolio_result, watchlist_quotes, question)

    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        # effort medium: a latency/cost balance that suits an interactive desktop app;
        # bump to "high" for deeper analysis.
        output_config={"effort": "medium"},
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        for text in stream.text_stream:
            yield text
