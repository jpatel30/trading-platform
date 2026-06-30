"""
Mark-to-Market Engine.

Calculates current value of stored recommendations (options + stocks)
for the History tab and backtest dataset.

Lazy-refresh model: triggered when History tab loads, only re-marks
if existing marks are >15 min stale. No separate scheduler needed.
"""
from datetime import datetime, timezone


def get_current_option_value(ticker: str, legs: list[dict]) -> float | None:
    """
    Fetch current value of an option spread given ticker + stored legs.
    Matches each leg's strike+type to live contract data from same expiry.
    Returns current cost-to-enter this exact spread (same BUY/SELL convention
    as original entry_debit calculation) — None if any leg can't be matched.
    """
    if not legs or not ticker:
        return None

    try:
        from app.options_flow.unusual_whales import get_option_contracts

        expiry = legs[0].get("expiry", "")
        if not expiry:
            return None

        contracts = get_option_contracts(ticker, expiry=expiry, limit=200)
        if not contracts:
            return None

        # Build lookup: (strike, type_letter) -> mid price
        # UW returns option_symbol like "AFRM260717C00085000" (no separate strike/type fields)
        # Format: TICKER + YYMMDD + C/P + strike*1000 (8 digits)
        contract_map = {}
        for c in contracts:
            sym = c.get("option_symbol", "")
            bid = float(c.get("nbbo_bid", 0) or c.get("bid", 0) or 0)
            ask = float(c.get("nbbo_ask", 0) or c.get("ask", 0) or 0)
            mid = (bid + ask) / 2 if (bid and ask) else float(c.get("mid", 0) or 0)
            if not sym or not mid:
                continue
            try:
                # Find C or P marker, strike is the 8 digits after it
                for marker in ("C", "P"):
                    idx = sym.rfind(marker)
                    if idx > 0 and sym[idx+1:].isdigit() and len(sym[idx+1:]) == 8:
                        strike = int(sym[idx+1:]) / 1000.0
                        contract_map[(strike, marker)] = mid
                        break
            except Exception:
                continue

        # Match legs with small tolerance for float precision
        def _find_match(strike, type_key):
            if (strike, type_key) in contract_map:
                return contract_map[(strike, type_key)]
            for (s, t), m in contract_map.items():
                if t == type_key and abs(s - strike) < 0.01:
                    return m
            return None

        total_value = 0.0
        matched     = 0
        for leg in legs:
            strike   = round(float(leg.get("strike", 0) or 0), 2)
            type_key = (leg.get("type", "")).upper()[:1]
            action   = leg.get("action", "")

            current_mid = _find_match(strike, type_key)
            if current_mid is None:
                continue
            matched += 1

            if action == "BUY":
                total_value += current_mid
            elif action == "SELL":
                total_value -= current_mid

        if matched < len(legs):
            return None  # couldn't price every leg — unreliable mark

        return round(total_value, 2)

    except Exception as e:
        print(f"[MarkToMarket] Option value error for {ticker}: {e}")
        return None


def get_current_stock_value(ticker: str) -> float | None:
    """Get current stock price for mark-to-market."""
    try:
        from app.options_flow.unusual_whales import get_stock_state
        state = get_stock_state(ticker)
        if state and state.get("price"):
            return float(state["price"])
    except Exception:
        pass
    return None


def mark_recommendation(rec: dict, is_market_open: bool) -> dict:
    """
    Calculate current value + P&L for a single recommendation.
    Handles both debit spreads (positive entry_debit) and
    credit spreads (negative entry_debit) correctly.
    """
    ticker      = rec.get("ticker", "")
    legs        = rec.get("legs") or []
    entry_debit = float(rec.get("entry_debit", 0) or 0)
    entry_low   = float(rec.get("entry_zone_low", 0) or 0)

    result = {
        "current_value": None,
        "pnl_dollars":   None,
        "pnl_pct":       None,
        "mark_type":     "live" if is_market_open else "eod_close",
    }

    if legs:
        # OPTIONS — match same BUY/SELL convention as entry_debit
        current_value = get_current_option_value(ticker, legs)
        if current_value is not None and entry_debit:
            result["current_value"] = current_value
            if entry_debit > 0:
                # Debit spread: paid entry_debit, profit when value rises
                pnl_per_share = current_value - entry_debit
            else:
                # Credit spread: received |entry_debit|, profit when cost to close falls
                pnl_per_share = abs(entry_debit) - current_value
            result["pnl_dollars"] = round(pnl_per_share * 100, 2)  # per contract
            result["pnl_pct"] = round((pnl_per_share / abs(entry_debit)) * 100, 1) if entry_debit else None
    else:
        # STOCKS
        current_price = get_current_stock_value(ticker)
        if current_price is not None and entry_low:
            result["current_value"] = current_price
            pnl_per_share = current_price - entry_low
            result["pnl_dollars"] = round(pnl_per_share, 2)  # per share
            result["pnl_pct"] = round((pnl_per_share / entry_low) * 100, 1) if entry_low else None

    return result


def mark_all_active_recommendations(user_id: str, days_back: int = 90) -> dict:
    """
    Mark all recent recommendations to market.
    Called lazily when History tab loads and existing marks are stale.
    """
    from sqlalchemy import text
    from app.db.session import get_session
    from app.scanner.quick_scan import _is_market_open

    is_open      = _is_market_open()
    marked_count = 0
    error_count  = 0

    try:
        with get_session() as s:
            rows = s.execute(text("""
                SELECT id, ticker, legs, entry_debit, entry_zone_low
                FROM daily_recommendations
                WHERE user_id = :uid
                  AND date >= CURRENT_DATE - :days
                  AND status != 'INVALIDATED'
            """), {"uid": user_id, "days": days_back}).fetchall()

        for row in rows:
            rec = {
                "ticker":          row.ticker,
                "legs":            row.legs or [],
                "entry_debit":     float(row.entry_debit or 0),
                "entry_zone_low":  float(row.entry_zone_low or 0),
            }
            mark = mark_recommendation(rec, is_open)

            if mark["current_value"] is not None:
                try:
                    with get_session() as s:
                        s.execute(text("""
                            UPDATE daily_recommendations
                            SET current_value = :cv,
                                current_pnl_dollars = :pnld,
                                current_pnl_pct = :pnlp,
                                last_marked_at = now(),
                                mark_type = :mt
                            WHERE id = :id
                        """), {
                            "cv": mark["current_value"], "pnld": mark["pnl_dollars"],
                            "pnlp": mark["pnl_pct"], "mt": mark["mark_type"], "id": row.id,
                        })
                    marked_count += 1
                except Exception as e:
                    print(f"[MarkToMarket] DB update failed for {row.ticker}: {e}")
                    error_count += 1
            else:
                error_count += 1

        print(f"[MarkToMarket] Marked {marked_count}/{len(rows)} recs "
              f"(market {'open' if is_open else 'closed'})")
        return {"marked": marked_count, "errors": error_count,
                "total": len(rows), "market_open": is_open}

    except Exception as e:
        print(f"[MarkToMarket] Job failed: {e}")
        return {"marked": 0, "errors": 1, "total": 0, "error": str(e)}


def calculate_backtest_stats(user_id: str, days_back: int = 90) -> dict:
    """
    Aggregate stats from marked recommendations — feeds learning/calibration.
    Answers: is conviction scoring actually predictive? Which strategies win?
    """
    from sqlalchemy import text
    from app.db.session import get_session

    with get_session() as s:
        rows = s.execute(text("""
            SELECT ticker, direction, strategy, horizon, conviction_score,
                   conviction_tier, current_pnl_pct, current_pnl_dollars, date
            FROM daily_recommendations
            WHERE user_id = :uid AND date >= CURRENT_DATE - :days
              AND current_pnl_pct IS NOT NULL
        """), {"uid": user_id, "days": days_back}).fetchall()

    if not rows:
        return {"available": False, "reason": "No marked recommendations yet"}

    def _agg(rows, key_fn):
        groups: dict = {}
        for r in rows:
            k = key_fn(r) or "UNKNOWN"
            groups.setdefault(k, []).append(float(r.current_pnl_pct))
        return {
            k: {
                "count":       len(pnls),
                "win_rate":    round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
                "avg_pnl_pct": round(sum(pnls) / len(pnls), 1),
            }
            for k, pnls in groups.items()
        }

    tier_stats     = _agg(rows, lambda r: r.conviction_tier)
    strategy_stats = _agg(rows, lambda r: r.strategy)
    horizon_stats  = _agg(rows, lambda r: r.horizon)

    all_pnls = [float(r.current_pnl_pct) for r in rows]
    overall_win_rate = round(sum(1 for p in all_pnls if p > 0) / len(all_pnls) * 100, 1)
    overall_avg_pnl  = round(sum(all_pnls) / len(all_pnls), 1)

    insights = []
    if "HIGH" in tier_stats and "MODERATE" in tier_stats:
        hi, mo = tier_stats["HIGH"]["win_rate"], tier_stats["MODERATE"]["win_rate"]
        if hi > mo + 10:
            insights.append(f"Conviction scoring well-calibrated: HIGH wins {hi}% vs MODERATE {mo}%")
        elif hi < mo:
            insights.append(f"⚠️ HIGH tier ({hi}%) underperforming MODERATE ({mo}%) — scoring may need review")
    if strategy_stats:
        best = max(strategy_stats.items(), key=lambda x: x[1]["win_rate"])
        insights.append(f"Best strategy: {best[0]} ({best[1]['win_rate']}% win, {best[1]['count']} trades)")

    return {
        "available":             True,
        "total_recommendations": len(rows),
        "overall_win_rate":      overall_win_rate,
        "overall_avg_pnl_pct":   overall_avg_pnl,
        "by_conviction_tier":    tier_stats,
        "by_strategy":           strategy_stats,
        "by_horizon":            horizon_stats,
        "insight":               " | ".join(insights) if insights else "Gathering more data for reliable insights",
    }
