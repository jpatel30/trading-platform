"""
Add MCP tools for History/backtest to app/mcp_server/server.py
Run from trading-platform root: python3 apply_history_mcp.py
"""

content = open('app/mcp_server/server.py').read()

new_tools = '''

@mcp.tool()
def get_recommendation_history_detailed(days_back: int = 30) -> dict:
    """
    Get full recommendation history grouped by date with mark-to-market P&L.
    Shows every recommendation given, entry value vs current value, win/loss.
    Use this to answer "how have my recommendations performed?"
    """
    from app.recommendations.mark_to_market import mark_all_active_recommendations, calculate_backtest_stats
    from app.utils.current_user import get_current_user_id
    from sqlalchemy import text
    from app.db.session import get_session

    user_id = get_current_user_id()
    mark_all_active_recommendations(user_id, days_back)

    with get_session() as s:
        rows = s.execute(text("""
            SELECT ticker, direction, strategy, horizon, conviction_score,
                   conviction_tier, entry_debit, entry_zone_low,
                   current_value, current_pnl_dollars, current_pnl_pct,
                   mark_type, date
            FROM daily_recommendations
            WHERE user_id=:uid AND date >= CURRENT_DATE - :days
            ORDER BY date DESC, conviction_score DESC
        """), {"uid": user_id, "days": days_back}).fetchall()

    by_date: dict = {}
    for r in rows:
        d = str(r.date)
        by_date.setdefault(d, []).append({
            "ticker": r.ticker, "direction": r.direction, "strategy": r.strategy,
            "conviction": r.conviction_score, "entry": float(r.entry_debit or r.entry_zone_low or 0),
            "current": float(r.current_value) if r.current_value is not None else None,
            "pnl_dollars": float(r.current_pnl_dollars) if r.current_pnl_dollars is not None else None,
            "pnl_pct": float(r.current_pnl_pct) if r.current_pnl_pct is not None else None,
            "mark_type": r.mark_type,
        })

    return {"by_date": by_date, "days_covered": len(by_date)}


@mcp.tool()
def get_backtest_stats(days_back: int = 90) -> dict:
    """
    Backtest statistics: win rate by conviction tier, strategy, and horizon.
    Use this to evaluate if conviction scoring is actually predictive,
    and which strategies have historically performed best.
    """
    from app.recommendations.mark_to_market import calculate_backtest_stats
    from app.utils.current_user import get_current_user_id
    return calculate_backtest_stats(get_current_user_id(), days_back)

'''

marker = "@mcp.tool()\ndef get_recommendation_history("
if marker in content:
    idx = content.find(marker)
    # Insert after the existing get_recommendation_history function ends
    # Find the next @mcp.tool() after this one
    next_tool = content.find("@mcp.tool()", idx + len(marker))
    content = content[:next_tool] + new_tools.strip() + "\n\n\n" + content[next_tool:]
    open('app/mcp_server/server.py', 'w').write(content)
    print('✅ MCP tools added after get_recommendation_history')
else:
    with open('app/mcp_server/server.py', 'a') as f:
        f.write(new_tools)
    print('✅ Appended MCP tools at end of file')

import ast
ast.parse(open('app/mcp_server/server.py').read())
print('✅ Syntax OK')
