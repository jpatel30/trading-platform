"""
Apply History tab API endpoints to app/api/main.py
Run from trading-platform root: python3 apply_history_api.py
"""

content = open('app/api/main.py').read()

new_endpoints = '''

@app.get("/api/recommendations/history-grouped", tags=["Recommendations"])
async def get_history_grouped(
    days_back: int = 30,
    force_remark: bool = False,
    user_id: str = Depends(get_current_user)
):
    """
    Recommendation history grouped by date with mark-to-market P&L.
    Lazy-refresh: re-marks if existing marks are >15 min stale.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        from app.recommendations.mark_to_market import mark_all_active_recommendations
        from datetime import datetime, timezone

        with get_session() as s:
            staleness = s.execute(text("""
                SELECT MIN(last_marked_at) as oldest_mark,
                       COUNT(*) FILTER (WHERE last_marked_at IS NULL) as unmarked
                FROM daily_recommendations
                WHERE user_id=:uid AND date >= CURRENT_DATE - :days
                  AND status != 'INVALIDATED'
            """), {"uid": user_id, "days": days_back}).fetchone()

        needs_remark = force_remark
        if staleness:
            if staleness.unmarked and staleness.unmarked > 0:
                needs_remark = True
            elif staleness.oldest_mark:
                age_min = (datetime.now(timezone.utc) - staleness.oldest_mark).total_seconds() / 60
                if age_min > 15:
                    needs_remark = True

        if needs_remark:
            mark_all_active_recommendations(user_id, days_back)

        with get_session() as s:
            rows = s.execute(text("""
                SELECT id, ticker, direction, strategy, horizon, expiry,
                       conviction_score, conviction_tier, thesis,
                       entry_debit, entry_zone_low, entry_zone_high,
                       current_value, current_pnl_dollars, current_pnl_pct,
                       mark_type, last_marked_at, status, date, created_at
                FROM daily_recommendations
                WHERE user_id = :uid AND date >= CURRENT_DATE - :days
                ORDER BY date DESC, conviction_score DESC
            """), {"uid": user_id, "days": days_back}).fetchall()

        grouped: dict = {}
        for r in rows:
            d = str(r.date)
            grouped.setdefault(d, []).append({
                "id":              str(r.id),
                "ticker":          r.ticker,
                "direction":       r.direction,
                "strategy":        r.strategy,
                "horizon":         r.horizon,
                "expiry":          str(r.expiry) if r.expiry else None,
                "conviction_score": r.conviction_score,
                "conviction_tier": r.conviction_tier,
                "thesis":          r.thesis,
                "entry_value":     float(r.entry_debit or r.entry_zone_low or 0),
                "current_value":   float(r.current_value) if r.current_value is not None else None,
                "pnl_dollars":     float(r.current_pnl_dollars) if r.current_pnl_dollars is not None else None,
                "pnl_pct":         float(r.current_pnl_pct) if r.current_pnl_pct is not None else None,
                "mark_type":       r.mark_type,
                "last_marked_at":  str(r.last_marked_at) if r.last_marked_at else None,
                "status":          r.status,
            })

        result = []
        for date, picks in sorted(grouped.items(), reverse=True):
            marked = [p for p in picks if p["pnl_dollars"] is not None]
            net_pnl = sum(p["pnl_dollars"] for p in marked)
            winners = sum(1 for p in marked if p["pnl_dollars"] > 0)
            losers  = sum(1 for p in marked if p["pnl_dollars"] < 0)
            result.append({
                "date":          date,
                "picks":         picks,
                "total_picks":   len(picks),
                "marked_picks":  len(marked),
                "net_pnl":       round(net_pnl, 2),
                "winners":       winners,
                "losers":        losers,
                "win_rate":      round(winners/len(marked)*100, 1) if marked else None,
            })

        return {"history": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/recommendations/backtest-stats", tags=["Recommendations"])
async def get_backtest_stats_endpoint(
    days_back: int = 90,
    user_id: str = Depends(get_current_user)
):
    try:
        from app.recommendations.mark_to_market import calculate_backtest_stats
        return calculate_backtest_stats(user_id, days_back)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

'''

marker = '@app.get("/api/recommendations/stocks", tags=["Recommendations"])'
if marker in content:
    content = content.replace(marker, new_endpoints.strip() + '\n\n\n' + marker)
    open('app/api/main.py', 'w').write(content)
    print('✅ History + backtest endpoints added')
else:
    print('❌ Marker not found — appending at end instead')
    with open('app/api/main.py', 'a') as f:
        f.write(new_endpoints)
    print('✅ Appended at end of file')

import ast
ast.parse(open('app/api/main.py').read())
print('✅ Syntax OK')
