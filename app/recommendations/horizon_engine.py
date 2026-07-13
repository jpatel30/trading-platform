"""
Phase B — Horizon-Aware Recommendation Engine.

Routes to options or stocks based on horizon.
Applies appropriate DTE, strategy selection, and scoring per timeframe.

Horizons:
    1w  → options 5-7 DTE   (swing trade — high conviction only ≥75)
    1m  → options 21-35 DTE (standard — conviction ≥70)
    3m  → options 60-90 DTE OR stock with 3-month thesis (conviction ≥65)
    6m  → stock primarily, LEAPS if available (conviction ≥60)
    1yr → stock only, fundamental thesis (conviction ≥55)

For 3m+: uses fundamental_score from fundamentals.py
For 1w/1m: uses conviction_score from conviction.py (options focus)

Pre-market: allowed, flagged as "next-day recommendation"

Rewritten July 2026 — removed SP500_SUPPLEMENT, a hardcoded 30-ticker
list unconditionally merged into every stock universe (regardless of
whether the user's actual watchlist already covered similar names).
Watchlist-only now: get_stock_universe() returns exactly the user's
watchlist + current portfolio positions, nothing else. Also found (and
removed by deleting the list) a real bug in that constant: "WDC",
"SNDK" was immediately followed by "BAC" with no comma between list
entries — adjacent Python string literals silently concatenate, so
that was actually one corrupted ticker "SNDKBAC", not two real ones.
"""
from datetime import datetime, date, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# Horizon Configuration
# ─────────────────────────────────────────────────────────────────────────────

HORIZON_CONFIG = {
    "1w":  {
        "type":           "options",
        "dte_min":        5,
        "dte_max":        9,
        "min_conviction": 75,
        "stop_pct":       -8,
        "label":          "1 Week",
        "description":    "Short-term swing trade — options expiring this week or next",
    },
    "2w":  {
        "type":           "options",
        "dte_min":        10,
        "dte_max":        16,
        "min_conviction": 72,
        "stop_pct":       -9,
        "label":          "2 Week",
        "description":    "Bi-weekly swing trade — 2-week options",
    },
    "1m":  {
        "type":           "options",
        "dte_min":        21,
        "dte_max":        35,
        "min_conviction": 70,
        "stop_pct":       -10,
        "label":          "1 Month",
        "description":    "Monthly options — standard recommendation timeframe",
    },
    "3m":  {
        "type":           "both",
        "dte_min":        60,
        "dte_max":        90,
        "min_conviction": 65,
        "min_fundamental": 55,
        "stop_pct":       -8,
        "label":          "3 Month",
        "description":    "Quarterly — options LEAPS-lite or stock with catalyst",
    },
    "6m":  {
        "type":           "stock",
        "min_fundamental": 60,
        "stop_pct":       -12,
        "label":          "6 Month",
        "description":    "Semi-annual stock pick — institutional accumulation focus",
    },
    "1yr": {
        "type":           "stock",
        "min_fundamental": 55,
        "stop_pct":       -15,
        "label":          "1 Year",
        "description":    "Annual stock thesis — fundamental growth focus",
    },
}

STOCK_UPSIDE_TARGETS = {
    "3m":  0.20,
    "6m":  0.35,
    "1yr": 0.50,
}


# ─────────────────────────────────────────────────────────────────────────────
# Stock Universe (watchlist + portfolio only — no hardcoded supplement)
# ─────────────────────────────────────────────────────────────────────────────

def get_stock_universe(user_id: str) -> list[str]:
    """Watchlist + current portfolio positions. No hardcoded ticker list."""
    tickers = set()

    try:
        from app.broker.watchlist_sync import get_db_watchlist
        tickers.update(get_db_watchlist(user_id))
    except Exception:
        pass

    try:
        from app.broker.webull_connector import WebullConnector
        positions = WebullConnector(user_id).get_positions()
        tickers.update(p["symbol"] for p in positions)
    except Exception:
        pass

    return list(tickers)


# ─────────────────────────────────────────────────────────────────────────────
# Options Horizon Recommendation
# ─────────────────────────────────────────────────────────────────────────────

def get_options_for_horizon(
    ticker: str,
    horizon: str,
    budget: float,
    user_id: str | None = None,
) -> dict:
    """Build options recommendation for 1w / 1m / 3m horizon."""
    from app.strategy.engine import build_recommendation
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package
    from app.market_data.uw_market_data import get_bars
    from app.technical_analysis.engine import get_technical_profile

    config    = HORIZON_CONFIG.get(horizon, HORIZON_CONFIG["1m"])
    dte_min   = config["dte_min"]
    dte_max   = config["dte_max"]

    from_date = (datetime.now()-timedelta(days=300)).strftime('%Y-%m-%d')
    to_date   = datetime.now().strftime('%Y-%m-%d')
    bars      = get_bars(ticker, 1, 'day', from_date, to_date)
    ta        = get_technical_profile(ticker, bars) if bars else {}
    signal    = score_signal_package(get_signal_package(ticker))

    from app.strategy.engine import _get_all_expiries
    all_exp  = _get_all_expiries(ticker)
    today_dt = datetime.now()
    valid    = [
        (exp, (datetime.strptime(exp, "%Y-%m-%d") - today_dt).days)
        for exp in all_exp
        if dte_min <= (datetime.strptime(exp, "%Y-%m-%d") - today_dt).days <= dte_max
    ]
    if not valid:
        return {"error": f"No expiry found between {dte_min}-{dte_max} DTE for {ticker}"}
    best_exp, best_dte = sorted(valid, key=lambda x: abs(x[1] - (dte_min+dte_max)//2))[0]
    print(f"[Horizon] {ticker} {horizon}: using expiry {best_exp} ({best_dte} DTE)")

    rec = build_recommendation(
        ticker      = ticker,
        ta_profile  = ta,
        flow_signal = signal,
        budget      = budget,
        user_id     = user_id,
        min_dte     = best_dte - 1,
        max_dte     = best_dte + 1,
    )

    if rec:
        rec["horizon"]      = horizon
        rec["horizon_label"] = config["label"]
        rec["rec_type"]     = "options"
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# Stock Horizon Recommendation
# ─────────────────────────────────────────────────────────────────────────────

def get_stock_for_horizon(
    ticker: str,
    horizon: str,
    budget: float,
    current_price: float | None = None,
) -> dict:
    """Build stock recommendation for 3m / 6m / 1yr horizon."""
    from app.recommendations.fundamentals import (
        get_fundamentals, get_dp_accumulation_score, score_fundamentals
    )

    config = HORIZON_CONFIG.get(horizon, HORIZON_CONFIG["6m"])

    if not current_price:
        try:
            import yfinance as yf
            current_price = yf.Ticker(ticker).fast_info.last_price
        except Exception:
            current_price = 0

    if not current_price:
        return {"error": "Could not get current price"}

    fundamentals = get_fundamentals(ticker)
    dp           = get_dp_accumulation_score(ticker)
    fund_score   = score_fundamentals(fundamentals, dp, current_price)

    min_fund = config.get("min_fundamental", 55)
    if fund_score["fundamental_score"] < min_fund:
        return {
            "filtered": True,
            "reason":   f"Fundamental score {fund_score['fundamental_score']}/100 below {min_fund} threshold",
            "ticker":   ticker,
            "horizon":  horizon,
        }

    momentum = _get_momentum(ticker, horizon)

    analyst_target = fundamentals.get("target_mean_price")
    momentum_target = current_price * (1 + STOCK_UPSIDE_TARGETS.get(horizon, 0.25))

    if analyst_target and analyst_target > current_price:
        target_price = max(analyst_target, momentum_target)
        target_source = "analyst + momentum"
    else:
        target_price = momentum_target
        target_source = "momentum"

    target_pct = round((target_price - current_price) / current_price * 100, 1)

    stop_pct   = config["stop_pct"]
    stop_price = round(current_price * (1 + stop_pct / 100), 2)

    shares = int(budget / current_price)
    if shares < 1:
        shares = 1
    total_cost = round(shares * current_price, 2)

    thesis = _generate_stock_thesis(
        ticker, horizon, config, fundamentals, dp, fund_score,
        current_price, target_price, target_pct, momentum
    )

    try:
        from app.llm.service import _call_ollama, is_ollama_available
        if is_ollama_available():
            prompt = (
                f"Write a 2-sentence investment thesis for {ticker} {horizon} stock pick. "
                f"Facts: price ${current_price:.2f}, analyst target ${target_price:.2f} "
                f"({target_pct:+.1f}%), revenue growth {(fundamentals.get('revenue_growth') or 0)*100:.0f}%, "
                f"PEG {fundamentals.get('peg_ratio') or 'N/A'}, "
                f"margins {(fundamentals.get('profit_margins') or 0)*100:.0f}%, "
                f"analyst rec: {fundamentals.get('analyst_recommendation','N/A')}. "
                f"Be specific and actionable."
            )
            llm_thesis = _call_ollama(prompt=prompt,
                system="Expert stock analyst. 2 sentences max. No disclaimers.",
                max_tokens=100)
            if llm_thesis and len(llm_thesis) > 20:
                thesis = llm_thesis.strip()
    except Exception:
        pass

    return {
        "ticker":            ticker,
        "horizon":           horizon,
        "horizon_label":     config["label"],
        "rec_type":          "stock",
        "direction":         "BULLISH",
        "fundamental_score": fund_score["fundamental_score"],
        "fundamental_breakdown": fund_score["breakdown"],
        "dp_score":          dp["score"],
        "dp_note":           dp["note"],
        "analyst_target":    analyst_target,
        "analyst_rec":       fundamentals.get("analyst_recommendation"),
        "analyst_count":     fundamentals.get("analyst_count"),
        "revenue_growth":    fundamentals.get("revenue_growth"),
        "peg_ratio":         fundamentals.get("peg_ratio"),
        "profit_margins":    fundamentals.get("profit_margins"),
        "current_price":     current_price,
        "entry_price":       current_price,
        "target_price":      round(target_price, 2),
        "target_pct":        target_pct,
        "target_source":     target_source,
        "stop_price":        stop_price,
        "stop_pct":          stop_pct,
        "shares":            shares,
        "total_cost":        total_cost,
        "potential_gain":    round((target_price - current_price) * shares, 2),
        "potential_loss":    round((stop_price - current_price) * shares, 2),
        "risk_reward":       round(abs(target_pct / stop_pct), 2),
        "thesis":             thesis,
        "momentum_score":     momentum.get("score", 50),
        "momentum_note":      momentum.get("note", ""),
        "invalidation_conditions": (
            f"{ticker} closes below ${stop_price} ({stop_pct}% stop) | "
            f"Analyst consensus downgrades to HOLD/SELL | "
            f"Revenue growth decelerates below 15%"
        ),
    }


def _get_momentum(ticker: str, horizon: str) -> dict:
    try:
        import yfinance as yf
        lookback = {"3m": "3mo", "6m": "6mo", "1yr": "1y"}.get(horizon, "3mo")
        hist     = yf.Ticker(ticker).history(period=lookback)
        if hist.empty:
            return {"score": 50, "note": "No price history"}

        start_price = hist["Close"].iloc[0]
        end_price   = hist["Close"].iloc[-1]
        ret_pct     = (end_price - start_price) / start_price * 100

        if ret_pct >= 30:
            score, note = 90, f"Strong uptrend +{ret_pct:.1f}% over {lookback}"
        elif ret_pct >= 15:
            score, note = 70, f"Positive momentum +{ret_pct:.1f}% over {lookback}"
        elif ret_pct >= 5:
            score, note = 55, f"Moderate gain +{ret_pct:.1f}% over {lookback}"
        elif ret_pct >= -5:
            score, note = 45, f"Flat {ret_pct:.1f}% over {lookback}"
        elif ret_pct >= -15:
            score, note = 35, f"Weak -{abs(ret_pct):.1f}% over {lookback} — check thesis"
        else:
            score, note = 20, f"Downtrend -{abs(ret_pct):.1f}% over {lookback} — caution"

        return {"score": score, "return_pct": round(ret_pct, 1), "note": note}
    except Exception:
        return {"score": 50, "note": "Momentum data unavailable"}


def _generate_stock_thesis(
    ticker: str, horizon: str, config: dict,
    fundamentals: dict, dp: dict, fund_score: dict,
    current_price: float, target_price: float, target_pct: float,
    momentum: dict,
) -> str:
    rev_g   = fundamentals.get("revenue_growth", 0) or 0
    peg     = fundamentals.get("peg_ratio")
    margins = fundamentals.get("profit_margins", 0) or 0
    analyst = fundamentals.get("analyst_recommendation", "N/A")
    n_analysts = fundamentals.get("analyst_count", 0)

    parts = [
        f"{ticker} {config['label']} thesis ({config['description']}).",
        f"Analyst consensus: {analyst} across {n_analysts} analysts — "
        f"mean target ${target_price:.0f} ({target_pct:+.1f}% upside).",
    ]

    if rev_g > 0:
        parts.append(f"Revenue growing {rev_g*100:.0f}% YoY.")
    if peg and peg < 1.5:
        parts.append(f"PEG {peg:.2f} — trading cheaply relative to growth.")
    if margins > 0:
        parts.append(f"Strong margins at {margins*100:.0f}%.")
    if dp.get("score", 50) >= 60:
        parts.append(f"Dark pool shows institutional accumulation ({dp['note']}).")

    parts.append(
        f"Momentum: {momentum.get('note', 'N/A')}. "
        f"Entry near ${current_price:.2f}, target ${target_price:.2f}, "
        f"stop ${current_price * (1 + config['stop_pct']/100):.2f} ({config['stop_pct']}%)."
    )

    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Unified Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def get_horizon_recommendation(
    ticker: str,
    horizon: str,
    budget:  float = 2000,
    user_id: str | None = None,
) -> dict:
    config = HORIZON_CONFIG.get(horizon)
    if not config:
        return {"error": f"Unknown horizon '{horizon}'. Valid: {list(HORIZON_CONFIG.keys())}"}

    market_open  = _is_market_open()
    next_day_flag = not market_open

    result = {
        "ticker":      ticker,
        "horizon":     horizon,
        "label":       config["label"],
        "description": config["description"],
        "market_open": market_open,
        "next_day":    next_day_flag,
    }

    if next_day_flag:
        result["market_note"] = (
            "Market is closed — this recommendation is for next trading session. "
            "Verify price and entry trigger before executing."
        )

    rec_type = config["type"]

    if rec_type == "options":
        rec = get_options_for_horizon(ticker, horizon, budget, user_id)
        result["options_rec"] = rec
        result["primary_rec"] = "options"

    elif rec_type == "stock":
        rec = get_stock_for_horizon(ticker, horizon, budget)
        result["stock_rec"] = rec
        result["primary_rec"] = "stock"

    elif rec_type == "both":
        options_rec = get_options_for_horizon(ticker, horizon, budget, user_id)
        stock_rec   = get_stock_for_horizon(ticker, horizon, budget)
        result["options_rec"] = options_rec
        result["stock_rec"]   = stock_rec

        options_conf = options_rec.get("confidence", 0) if options_rec else 0
        stock_fund   = stock_rec.get("fundamental_score", 0) if stock_rec else 0

        if options_conf >= 65 and stock_fund >= 60:
            result["primary_rec"]    = "both"
            result["primary_note"]   = "Both options and stock look strong — options for short profit, stock for compounding"
        elif options_conf >= 65:
            result["primary_rec"]    = "options"
        else:
            result["primary_rec"]    = "stock"

    return result


def scan_for_horizon(
    horizon: str,
    budget:  float = 2000,
    top_n:   int   = 5,
    user_id: str | None = None,
) -> dict:
    """Scan the user's actual watchlist + portfolio for horizon recommendations."""
    import time

    config     = HORIZON_CONFIG.get(horizon, HORIZON_CONFIG["1m"])
    rec_type   = config["type"]
    t0         = time.time()

    if user_id:
        tickers = get_stock_universe(user_id)
    else:
        tickers = []
        print("[HorizonScan] No user_id — cannot resolve a watchlist, returning empty universe")

    print(f"[HorizonScan] {horizon} — scanning {len(tickers)} tickers...")

    results  = []
    filtered = []

    for ticker in tickers:
        try:
            rec = get_horizon_recommendation(ticker, horizon, budget, user_id)

            if rec_type in ("options", "both"):
                options_rec = rec.get("options_rec", {})
                conf = options_rec.get("confidence", 0) if options_rec else 0
                min_conf = config["min_conviction"]
                if conf < min_conf:
                    filtered.append({
                        "ticker": ticker,
                        "reason": f"Options confidence {conf} < {min_conf}"
                    })
                    continue

            if rec_type in ("stock", "both"):
                stock_rec  = rec.get("stock_rec", {})
                if stock_rec and stock_rec.get("filtered"):
                    filtered.append({
                        "ticker": ticker,
                        "reason": stock_rec.get("reason", "Below threshold")
                    })
                    if rec_type == "stock":
                        continue

            results.append(rec)

        except Exception as e:
            print(f"[HorizonScan] {ticker} failed: {e}")
            continue

    def sort_key(r):
        if rec_type == "stock":
            return r.get("stock_rec", {}).get("fundamental_score", 0) if r.get("stock_rec") else 0
        else:
            return r.get("options_rec", {}).get("confidence", 0) if r.get("options_rec") else 0

    results.sort(key=sort_key, reverse=True)

    elapsed = round(time.time()-t0, 1)
    print(f"[HorizonScan] Done in {elapsed}s — {len(results)} passed, {len(filtered)} filtered")

    return {
        "horizon":       horizon,
        "label":         config["label"],
        "recommendations": results[:top_n],
        "filtered_count":  len(filtered),
        "total_scanned":   len(tickers),
        "elapsed":         elapsed,
        "date":            date.today().isoformat(),
    }


def _is_market_open() -> bool:
    try:
        import pytz
        from datetime import time as dtime
        et  = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if now.weekday() >= 5:
            return False
        t = now.time()
        return dtime(9, 30) <= t <= dtime(16, 0)
    except Exception:
        return True
