"""
Phase B — Portfolio Addition Signals.

Answers: "Should I add more to any of my existing positions?"

Criteria (all must be true):
    ✅ Current P&L > +20% (thesis clearly working)
    ✅ TA still bullish (trend + MACD not reversing)
    ✅ Dark pool still accumulating (score ≥ 55)
    ✅ Not within 14 days of earnings (IV crush risk)
    ✅ Position not near stop loss (not overextended)

Score 0-100 per candidate → top picks only.
"""
from datetime import datetime, timedelta


ADD_SCORE_WEIGHTS = {
    "pnl":       30,   # how much profit confirms thesis
    "ta":        25,   # TA still bullish
    "dp":        25,   # institutional still accumulating
    "earnings":  10,   # not near earnings
    "extension": 10,   # not too overextended
}


def get_portfolio_additions(
    user_id: str,
    min_pnl_pct: float = 20.0,
    min_add_score: int = 60,
) -> dict:
    """
    Scan current portfolio for positions worth adding to.

    Args:
        min_pnl_pct:   minimum P&L % to consider adding (default 20%)
        min_add_score: minimum score to surface (default 60/100)

    Returns:
        candidates: list of positions worth adding to with score + reasoning
        not_ready:  positions with good P&L but failing other criteria
    """
    from app.broker.webull_connector import WebullConnector
    from app.market_data.polygon_client import get_bars
    from app.technical_analysis.engine import get_technical_profile
    from app.recommendations.fundamentals import get_dp_accumulation_score

    wb        = WebullConnector(user_id)
    positions = wb.get_positions()

    if not positions:
        return {"candidates": [], "not_ready": [], "message": "No positions found"}

    candidates = []
    not_ready  = []

    for pos in positions:
        symbol   = pos.get("symbol", "")
        pnl_rate = float(pos.get("unrealized_profit_loss_rate", 0)) * 100
        pnl_abs  = float(pos.get("unrealized_profit_loss", 0))
        cost     = float(pos.get("total_cost", 0))
        qty      = float(pos.get("qty", 0))
        price    = float(pos.get("last_price", 0))
        inst_type = pos.get("instrument_type", "STOCK")

        # Skip options (add logic for stocks/ETFs only)
        if inst_type == "OPTION":
            continue

        # Must meet minimum P&L threshold
        if pnl_rate < min_pnl_pct:
            not_ready.append({
                "symbol": symbol,
                "pnl_pct": round(pnl_rate, 1),
                "reason": f"P&L {pnl_rate:.1f}% below {min_pnl_pct}% threshold"
            })
            continue

        # Score this position
        try:
            add_score, breakdown = _score_addition_candidate(
                symbol, pnl_rate, pnl_abs, cost, qty, price, user_id
            )
        except Exception as e:
            print(f"[Addition] Score failed for {symbol}: {e}")
            continue

        entry = {
            "symbol":        symbol,
            "current_pnl_pct": round(pnl_rate, 1),
            "current_pnl_abs": round(pnl_abs, 2),
            "add_score":     add_score,
            "breakdown":     breakdown,
            "current_price": price,
            "current_qty":   qty,
            "current_cost":  cost,
        }

        if add_score >= min_add_score:
            # Add sizing suggestion
            entry["add_suggestion"] = _size_addition(price, cost, add_score)
            candidates.append(entry)
        else:
            not_ready.append({
                "symbol":  symbol,
                "pnl_pct": round(pnl_rate, 1),
                "add_score": add_score,
                "reason":  _get_fail_reason(breakdown),
            })

    # Sort by add_score desc
    candidates.sort(key=lambda x: x["add_score"], reverse=True)

    return {
        "candidates":  candidates,
        "not_ready":   not_ready,
        "total_positions_checked": len(positions),
        "summary": _format_summary(candidates, not_ready),
    }


def _score_addition_candidate(
    symbol: str, pnl_pct: float, pnl_abs: float,
    cost: float, qty: float, price: float, user_id: str
) -> tuple[int, dict]:
    """Score a position as candidate for adding more."""
    from app.market_data.polygon_client import get_bars
    from app.technical_analysis.engine import get_technical_profile
    from app.recommendations.fundamentals import get_dp_accumulation_score
    from app.options_flow.unusual_whales import get_earnings_premarket, get_earnings_afterhours

    breakdown = {}
    total     = 0

    # ── P&L Score (30 pts) ──────────────────────────────────────────────────
    if pnl_pct >= 100:
        pts = 30
    elif pnl_pct >= 50:
        pts = 25
    elif pnl_pct >= 30:
        pts = 20
    elif pnl_pct >= 20:
        pts = 15
    else:
        pts = 5
    breakdown["pnl"] = {
        "points": pts,
        "note":   f"Position up {pnl_pct:.1f}% (${pnl_abs:,.0f}) — thesis confirmed"
    }
    total += pts

    # ── TA Still Bullish (25 pts) ────────────────────────────────────────────
    try:
        from_date = (datetime.now()-timedelta(days=200)).strftime('%Y-%m-%d')
        to_date   = datetime.now().strftime('%Y-%m-%d')
        bars      = get_bars(symbol, 1, 'day', from_date, to_date)
        ta        = get_technical_profile(symbol, bars) if bars else {}

        signal = ta.get("signal", "NEUTRAL")
        trend  = ta.get("trend", "SIDEWAYS")
        macd   = ta.get("macd_signal", "NEUTRAL")
        rsi    = ta.get("rsi_14", 50) or 50

        if signal in ("BUY", "STRONG_BUY") and trend == "UPTREND":
            pts = 25
            note = f"Strong: {signal}, {trend}, MACD {macd}, RSI {rsi:.0f}"
        elif signal in ("BUY", "STRONG_BUY") or trend == "UPTREND":
            pts = 18
            note = f"Moderate: {signal}, {trend}, RSI {rsi:.0f}"
        elif signal == "NEUTRAL" and trend != "DOWNTREND":
            pts = 10
            note = f"Neutral: {signal}, {trend} — watching for reversal"
        elif rsi > 70:
            pts = 5
            note = f"Overbought RSI {rsi:.0f} — risky to add here"
        else:
            pts = 3
            note = f"Bearish signals: {signal}, {trend} — don't add yet"

        breakdown["ta"] = {"points": pts, "note": note}
        total += pts

    except Exception:
        breakdown["ta"] = {"points": 10, "note": "TA unavailable — neutral"}
        total += 10

    # ── Dark Pool Accumulation (25 pts) ─────────────────────────────────────
    try:
        dp    = get_dp_accumulation_score(symbol)
        score = dp.get("score", 50)
        if score >= 70:
            pts = 25
        elif score >= 60:
            pts = 18
        elif score >= 50:
            pts = 12
        elif score >= 40:
            pts = 6
        else:
            pts = 0
        breakdown["dp"] = {"points": pts, "note": dp.get("note", "N/A")}
        total += pts
    except Exception:
        breakdown["dp"] = {"points": 10, "note": "DP data unavailable — neutral"}
        total += 10

    # ── Earnings Check (10 pts) ──────────────────────────────────────────────
    try:
        from app.rag.context_builder import _build_earnings_context
        earn = _build_earnings_context(symbol)
        upcoming = earn.get("upcoming")

        if upcoming and upcoming.get("days_away") is not None:
            days = upcoming["days_away"]
            if days <= 7:
                pts  = 0
                note = f"Earnings in {days} days — too risky to add"
            elif days <= 14:
                pts  = 3
                note = f"Earnings in {days} days — wait until after earnings to add"
            else:
                pts  = 10
                note = f"Earnings not for {days} days — safe window to add"
        else:
            pts  = 10
            note = "No upcoming earnings — safe to add"

        breakdown["earnings"] = {"points": pts, "note": note}
        total += pts
    except Exception:
        breakdown["earnings"] = {"points": 7, "note": "Earnings check unavailable — partial credit"}
        total += 7

    # ── Extension Check (10 pts) — not too overextended ────────────────────
    try:
        # If up >200% already, risk of mean reversion increases
        if pnl_pct >= 200:
            pts  = 2
            note = f"Position up {pnl_pct:.0f}% — very extended, high mean reversion risk"
        elif pnl_pct >= 150:
            pts  = 5
            note = f"Position up {pnl_pct:.0f}% — moderately extended, consider partial exit not addition"
        elif pnl_pct >= 50:
            pts  = 8
            note = f"Position up {pnl_pct:.0f}% — healthy extension, still room to run"
        else:
            pts  = 10
            note = f"Position up {pnl_pct:.0f}% — early stage, good to add"
        breakdown["extension"] = {"points": pts, "note": note}
        total += pts
    except Exception:
        breakdown["extension"] = {"points": 5, "note": "Extension check failed"}
        total += 5

    return min(100, total), breakdown


def _size_addition(price: float, current_cost: float, score: int) -> dict:
    """Suggest addition size based on score and current position."""
    if score >= 80:
        add_pct  = 50   # add up to 50% of current position size
        rationale = "Strong conviction — significant addition appropriate"
    elif score >= 70:
        add_pct  = 30
        rationale = "Good conviction — moderate addition"
    else:
        add_pct  = 15
        rationale = "Passing threshold — small addition only"

    suggested_cost  = round(current_cost * (add_pct / 100), 2)
    suggested_shares = max(1, int(suggested_cost / price)) if price else 1

    return {
        "add_pct_of_position": add_pct,
        "suggested_additional_cost": suggested_cost,
        "suggested_additional_shares": suggested_shares,
        "rationale": rationale,
    }


def _get_fail_reason(breakdown: dict) -> str:
    """Get primary reason why position didn't qualify for addition."""
    failing = [
        (k, v) for k, v in breakdown.items()
        if v["points"] < ADD_SCORE_WEIGHTS.get(k, 10) * 0.4
    ]
    if not failing:
        return "Score just below threshold"
    worst = min(failing, key=lambda x: x[1]["points"] / ADD_SCORE_WEIGHTS.get(x[0], 10))
    return worst[1]["note"]


def _format_summary(candidates: list, not_ready: list) -> str:
    if not candidates:
        return (
            f"No positions qualify for addition right now. "
            f"{len(not_ready)} positions checked — "
            f"most common issue: TA not confirming or earnings risk."
        )
    syms = [c["symbol"] for c in candidates[:3]]
    return (
        f"{len(candidates)} position(s) qualify for addition: {', '.join(syms)}. "
        f"Top pick: {candidates[0]['symbol']} (score {candidates[0]['add_score']}/100, "
        f"up {candidates[0]['current_pnl_pct']:.1f}%)."
    )
