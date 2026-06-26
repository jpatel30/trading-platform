"""
Phase B — Fundamental Scoring.

Scores stocks on fundamentals for 3m/6m/1yr recommendations.
Uses yfinance (analyst targets, growth, valuation) + dark pool accumulation.

Scoring (0-100):
    Analyst upside:      25pts  (targetMeanPrice vs current)
    Revenue growth:      20pts  (revenueGrowth YoY)
    PEG ratio:           20pts  (price/earnings-to-growth — cheaper = better)
    Profit margins:      15pts  (profitMargins — higher = better moat)
    DP accumulation:     20pts  (30-day dark pool buy/sell balance)

Anchors:
    Target = analyst mean price (yfinance) adjusted by our momentum score
    Stop   = -8% (3m) / -12% (6m) / -15% (1yr) from entry
"""
import time


def get_fundamentals(ticker: str) -> dict:
    """Fetch fundamental data from yfinance."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return {
            "current_price":          info.get("currentPrice"),
            "target_mean_price":      info.get("targetMeanPrice"),
            "target_high_price":      info.get("targetHighPrice"),
            "target_low_price":       info.get("targetLowPrice"),
            "analyst_recommendation": info.get("recommendationKey"),
            "analyst_count":          info.get("numberOfAnalystOpinions", 0),
            "revenue_growth":         info.get("revenueGrowth"),
            "earnings_growth":        info.get("earningsGrowth"),
            "profit_margins":         info.get("profitMargins"),
            "return_on_equity":       info.get("returnOnEquity"),
            "debt_to_equity":         info.get("debtToEquity"),
            "trailing_pe":            info.get("trailingPE"),
            "forward_pe":             info.get("forwardPE"),
            "peg_ratio":              info.get("pegRatio"),
            "price_to_book":          info.get("priceToBook"),
            "market_cap":             info.get("marketCap"),
            "sector":                 info.get("sector"),
            "industry":               info.get("industry"),
            "beta":                   info.get("beta"),
            "week_52_change":         info.get("52WeekChange"),
        }
    except Exception as e:
        return {"error": str(e)}


def get_dp_accumulation_score(ticker: str) -> dict:
    """
    Calculate institutional accumulation score from dark pool prints.
    Score 0-100: >50 = net buying, <50 = net selling.
    """
    try:
        from app.options_flow.unusual_whales import get_dark_pool_ticker
        prints = get_dark_pool_ticker(ticker)

        if not prints or not isinstance(prints, list):
            return {"score": 50, "note": "No dark pool data", "buy_count": 0, "sell_count": 0}

        buy_premium  = 0.0
        sell_premium = 0.0
        buy_count    = 0
        sell_count   = 0

        for p in prints:
            premium = float(p.get("premium", 0) or 0)
            price   = float(p.get("price", 0) or 0)
            bid     = float(p.get("nbbo_bid", price) or price)
            ask     = float(p.get("nbbo_ask", price) or price)
            mid     = (bid + ask) / 2

            # Classify: above mid = aggressive buy, below mid = aggressive sell
            if price >= mid and premium > 0:
                buy_premium += premium
                buy_count   += 1
            elif price < mid and premium > 0:
                sell_premium += premium
                sell_count   += 1

        total = buy_premium + sell_premium
        if total == 0:
            return {"score": 50, "note": "No classified prints", "buy_count": 0, "sell_count": 0}

        buy_ratio = buy_premium / total
        score     = round(buy_ratio * 100)

        if score >= 70:
            note = f"Strong institutional buying ({score:.0f}% premium on buy side)"
        elif score >= 55:
            note = f"Moderate accumulation ({score:.0f}% buy premium)"
        elif score <= 30:
            note = f"Institutional distribution ({score:.0f}% buy, heavy selling)"
        elif score <= 45:
            note = f"Moderate selling pressure ({score:.0f}% buy premium)"
        else:
            note = f"Balanced flow ({score:.0f}% buy premium)"

        return {
            "score":          score,
            "note":           note,
            "buy_count":      buy_count,
            "sell_count":     sell_count,
            "buy_premium":    round(buy_premium),
            "sell_premium":   round(sell_premium),
            "total_prints":   len(prints),
        }

    except Exception as e:
        return {"score": 50, "note": f"Error: {e}", "buy_count": 0, "sell_count": 0}


def score_fundamentals(
    fundamentals: dict,
    dp: dict,
    current_price: float | None = None,
) -> dict:
    """
    Score fundamentals 0-100 for stock recommendation.
    Higher = better stock pick for medium/long term.
    """
    if fundamentals.get("error"):
        return {"fundamental_score": 50, "error": fundamentals["error"]}

    price = current_price or fundamentals.get("current_price") or 0
    breakdown = {}
    total     = 0

    # ── Analyst target upside (25 pts) ──────────────────────────────────────
    target_mean = fundamentals.get("target_mean_price")
    if target_mean and price > 0:
        upside_pct = (target_mean - price) / price * 100
        if upside_pct >= 50:
            pts = 25
        elif upside_pct >= 30:
            pts = 20
        elif upside_pct >= 15:
            pts = 15
        elif upside_pct >= 5:
            pts = 8
        else:
            pts = 2
        breakdown["analyst_upside"] = {
            "score": round(upside_pct, 1),
            "points": pts,
            "note": f"{upside_pct:.1f}% to analyst mean ${target_mean:.0f} "
                    f"({fundamentals.get('analyst_count', 0)} analysts, "
                    f"{fundamentals.get('analyst_recommendation', 'N/A')})"
        }
        total += pts
    else:
        breakdown["analyst_upside"] = {"score": 0, "points": 0, "note": "No analyst targets"}

    # ── Revenue growth (20 pts) ──────────────────────────────────────────────
    rev_growth = fundamentals.get("revenue_growth")
    if rev_growth is not None:
        rev_pct = rev_growth * 100
        if rev_pct >= 50:
            pts = 20
        elif rev_pct >= 20:
            pts = 15
        elif rev_pct >= 10:
            pts = 10
        elif rev_pct >= 0:
            pts = 5
        else:
            pts = 0
        breakdown["revenue_growth"] = {
            "score": round(rev_pct, 1),
            "points": pts,
            "note": f"Revenue growth {rev_pct:.1f}% YoY"
        }
        total += pts
    else:
        breakdown["revenue_growth"] = {"score": 0, "points": 5, "note": "No data (neutral)"}
        total += 5

    # ── PEG ratio (20 pts) ──────────────────────────────────────────────────
    peg = fundamentals.get("peg_ratio")
    if peg is not None and peg > 0:
        if peg < 0.5:
            pts = 20
        elif peg < 1.0:
            pts = 15
        elif peg < 1.5:
            pts = 10
        elif peg < 2.0:
            pts = 5
        else:
            pts = 2
        breakdown["peg_ratio"] = {
            "score": round(peg, 2),
            "points": pts,
            "note": f"PEG {peg:.2f} ({'very cheap' if peg < 0.5 else 'cheap' if peg < 1 else 'fair' if peg < 1.5 else 'expensive'} relative to growth)"
        }
        total += pts
    else:
        breakdown["peg_ratio"] = {"score": 0, "points": 8, "note": "No PEG data (neutral)"}
        total += 8

    # ── Profit margins (15 pts) ──────────────────────────────────────────────
    margins = fundamentals.get("profit_margins")
    if margins is not None:
        margins_pct = margins * 100
        if margins_pct >= 40:
            pts = 15
        elif margins_pct >= 20:
            pts = 10
        elif margins_pct >= 10:
            pts = 7
        elif margins_pct >= 0:
            pts = 4
        else:
            pts = 0
        breakdown["profit_margins"] = {
            "score": round(margins_pct, 1),
            "points": pts,
            "note": f"Profit margin {margins_pct:.1f}%"
        }
        total += pts
    else:
        breakdown["profit_margins"] = {"score": 0, "points": 5, "note": "No margin data (neutral)"}
        total += 5

    # ── Dark pool accumulation (20 pts) ─────────────────────────────────────
    dp_score = dp.get("score", 50)
    if dp_score >= 70:
        pts = 20
    elif dp_score >= 60:
        pts = 15
    elif dp_score >= 50:
        pts = 10
    elif dp_score >= 40:
        pts = 5
    else:
        pts = 0
    breakdown["dp_accumulation"] = {
        "score": dp_score,
        "points": pts,
        "note": dp.get("note", "N/A")
    }
    total += pts

    # Cap at 100
    final = min(100, total)

    return {
        "fundamental_score": final,
        "breakdown":         breakdown,
        "analyst_upside_pct": round(
            (fundamentals.get("target_mean_price", price) - price) / price * 100, 1
        ) if price else None,
        "target_mean_price": fundamentals.get("target_mean_price"),
        "analyst_recommendation": fundamentals.get("analyst_recommendation"),
    }
