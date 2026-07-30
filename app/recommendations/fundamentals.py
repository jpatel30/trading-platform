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
            "quote_type":             info.get("quoteType", "EQUITY"),
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


def analyst_target_reliability(price: float, low: float, high: float, mean: float) -> float:
    """
    0.0-1.0 confidence multiplier for an analyst target, using only data
    already available from get_fundamentals()/_fetch_analyst_target() — no
    extra network calls.

      Price level:  thin analyst coverage, wide bid/ask spreads, and
                     outsized single-analyst-outlier risk make mean
                     targets on sub-$15 names systematically less
                     trustworthy. Ramps 0.35 (<=$3) to 1.0 (>=$15), not a
                     hard cutoff — a $12 stock still counts, just
                     partially discounted.
      Dispersion:   if analyst low/high disagree widely relative to the
                     mean, "consensus" is barely a consensus. Tight
                     agreement (<=30% spread/mean) = full trust; 150%+
                     spread = heavily discounted.

    Concrete motivating case: EVTL showed +517% upside on 5 analysts at a
    $1.62 share price — a mean target that thin and that close to zero is
    dominated by single-analyst-outlier risk, not a real consensus.
    Originally found and fixed only for smart_stock_scan.py's watchlist
    ranking; moved here so every caller of get_fundamentals() (single-
    ticker recommendations included) gets the same discount, not just
    watchlist-wide scans.
    """
    if not mean or mean <= 0:
        return 0.5

    if price >= 15:
        price_factor = 1.0
    elif price >= 3:
        price_factor = 0.35 + (price - 3) / 12 * 0.65
    else:
        price_factor = 0.35

    if low and high and high > low:
        spread_pct = (high - low) / mean
        dispersion_factor = max(0.3, 1.0 - max(0, spread_pct - 0.3) / 1.2)
    else:
        dispersion_factor = 0.6  # unknown spread — moderate discount, not full trust

    return round(price_factor * dispersion_factor, 3)


def get_fund_data(ticker: str) -> dict:
    """
    Fetch ETF/fund-specific data for score_etf_fundamentals(): expense
    ratio (+ category average), AUM, avg volume, holdings turnover
    (+ category average). ETFs don't have analyst targets/PEG/revenue
    growth, so score_fundamentals()'s rubric can't score them for real -
    this is the fund-appropriate equivalent of get_fundamentals().

    info["netExpenseRatio"] is already a plain percentage (0.0945 means
    0.0945%). funds_data.fund_operations reports the same figures as
    fractions (0.000945) - normalized to percentage points here so the
    two are directly comparable. Total Net Assets' "Category Average"
    column in funds_data mirrors the fund's own value (a yfinance data
    quirk, not a real peer average), so AUM uses the absolute info[]
    figure instead of a category comparison.
    """
    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        info = t.info
        result = {
            "expense_ratio": info.get("netExpenseRatio"),
            "total_assets":  info.get("totalAssets"),
            "avg_volume":    info.get("averageVolume"),
            "category":      info.get("category"),
        }
        try:
            fo = t.funds_data.fund_operations
            if fo is not None and not fo.empty and ticker in fo.columns:
                if "Annual Report Expense Ratio" in fo.index:
                    cat_er = fo.loc["Annual Report Expense Ratio", "Category Average"]
                    result["expense_ratio_cat_avg"] = float(cat_er) * 100
                    if result["expense_ratio"] is None:
                        result["expense_ratio"] = float(fo.loc["Annual Report Expense Ratio", ticker]) * 100
                if "Annual Holdings Turnover" in fo.index:
                    result["turnover"]        = float(fo.loc["Annual Holdings Turnover", ticker])
                    result["turnover_cat_avg"] = float(fo.loc["Annual Holdings Turnover", "Category Average"])
        except Exception:
            pass
        return result
    except Exception as e:
        return {"error": str(e)}


def score_etf_fundamentals(fund_data: dict, price: float = 0) -> dict:
    """
    Score an ETF/fund 0-100 on fund-appropriate metrics, replacing the
    old technicals-only stand-in (price vs 50d/52w-high, YoY change) that
    ran because ETFs have no analyst targets/PEG/revenue growth.

    Expense ratio vs category (30pts):    lower fees relative to peers.
    AUM (25pts):                          larger funds are more stable,
                                           tighter spreads, less
                                           delisting/closure risk.
    Liquidity, avg daily $ volume (20pts): easier to enter/exit near mid.
    Tracking fidelity proxy (25pts):      holdings turnover relative to
                                           category. Near-zero turnover is
                                           what a passive index-replicator
                                           should show; turnover far above
                                           category average suggests either
                                           active-style management or
                                           difficulty holding the index
                                           steady. This is a turnover-based
                                           proxy, not a measured tracking
                                           error against the fund's actual
                                           benchmark NAV series - yfinance
                                           doesn't expose that, so this is
                                           documented as an approximation
                                           rather than presented as exact.

    Same return shape as score_fundamentals() (fundamental_score,
    breakdown) so callers don't need to branch on the result shape.
    """
    if fund_data.get("error"):
        return {"fundamental_score": 50, "breakdown": {}, "error": fund_data["error"]}

    breakdown = {}
    total     = 0

    er     = fund_data.get("expense_ratio")
    er_cat = fund_data.get("expense_ratio_cat_avg")
    if er is not None:
        if er_cat and er_cat > 0:
            ratio = er / er_cat
            pts = 30 if ratio <= 0.3 else 22 if ratio <= 0.7 else 15 if ratio <= 1.0 else 8 if ratio <= 1.5 else 2
            note = f"{er:.2f}% expense ratio ({ratio:.2f}x category avg {er_cat:.2f}%)"
        else:
            pts = 26 if er <= 0.10 else 20 if er <= 0.25 else 12 if er <= 0.50 else 6 if er <= 1.0 else 2
            note = f"{er:.2f}% expense ratio (no category baseline)"
        breakdown["expense_ratio"] = {"score": er, "points": pts, "note": note}
        total += pts
    else:
        breakdown["expense_ratio"] = {"score": None, "points": 10, "note": "No expense ratio data (neutral)"}
        total += 10

    aum = fund_data.get("total_assets")
    if aum:
        pts = 25 if aum >= 50e9 else 20 if aum >= 10e9 else 14 if aum >= 1e9 else 7 if aum >= 100e6 else 2
        breakdown["aum"] = {"score": aum, "points": pts, "note": f"${aum/1e9:.1f}B AUM"}
        total += pts
    else:
        breakdown["aum"] = {"score": None, "points": 8, "note": "No AUM data (neutral)"}
        total += 8

    dollar_vol = (fund_data.get("avg_volume") or 0) * (price or 0)
    if dollar_vol:
        pts = 20 if dollar_vol >= 1e9 else 15 if dollar_vol >= 200e6 else 10 if dollar_vol >= 50e6 else 5 if dollar_vol >= 5e6 else 0
        breakdown["liquidity"] = {"score": dollar_vol, "points": pts, "note": f"${dollar_vol/1e6:.0f}M avg daily $ volume"}
        total += pts
    else:
        breakdown["liquidity"] = {"score": None, "points": 5, "note": "No volume data (neutral)"}
        total += 5

    turnover     = fund_data.get("turnover")
    turnover_cat = fund_data.get("turnover_cat_avg")
    if turnover is not None and turnover_cat:
        ratio = turnover / turnover_cat if turnover_cat > 0 else (0 if turnover == 0 else 2.0)
        pts = 25 if ratio <= 0.3 else 18 if ratio <= 0.7 else 12 if ratio <= 1.0 else 6 if ratio <= 1.5 else 2
        breakdown["tracking_fidelity"] = {
            "score": turnover, "points": pts,
            "note": f"{turnover*100:.1f}% annual turnover ({ratio:.2f}x category avg {turnover_cat*100:.1f}%) - "
                    f"proxy for index-tracking discipline, not measured tracking error"
        }
        total += pts
    else:
        breakdown["tracking_fidelity"] = {"score": None, "points": 12, "note": "No turnover data (neutral)"}
        total += 12

    return {"fundamental_score": min(100, round(total)), "breakdown": breakdown}


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
    # Discounted by reliability (price level + analyst low/high dispersion)
    # before scoring — an undiscounted mean target on a thin-coverage,
    # low-priced stock can show triple-digit "upside" that's really
    # single-analyst-outlier noise. See analyst_target_reliability().
    target_mean = fundamentals.get("target_mean_price")
    if target_mean and price > 0:
        raw_upside_pct = (target_mean - price) / price * 100
        reliability = analyst_target_reliability(
            price, fundamentals.get("target_low_price", 0) or 0,
            fundamentals.get("target_high_price", 0) or 0, target_mean,
        )
        upside_pct = raw_upside_pct * reliability
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
            "note": f"{upside_pct:.1f}% (reliability-discounted from {raw_upside_pct:.1f}%) "
                    f"to analyst mean ${target_mean:.0f} "
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

    # Bug fix: dict.get(key, default) only uses `default` when the key is
    # ABSENT — get_fundamentals() always includes "target_mean_price" in the
    # dict (as info.get("targetMeanPrice"), which is None for any ETF/fund),
    # so the old code returned None here, not price, and then crashed on
    # None - price. Explicit None-check instead.
    _target_mean = fundamentals.get("target_mean_price")
    return {
        "fundamental_score": final,
        "breakdown":         breakdown,
        "analyst_upside_pct": round(
            (_target_mean - price) / price * 100, 1
        ) if (price and _target_mean) else None,
        "target_mean_price": _target_mean,
        "analyst_recommendation": fundamentals.get("analyst_recommendation"),
    }
