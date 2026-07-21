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

# Option C decision this session: no conviction/fundamental gate tied to
# window length. Options already has real R/R + EV gates as its quality
# filter (strategy/engine.py::_execute_trade_math) regardless of window;
# stock has no direct equivalent, so it keeps ONE fixed floor instead of
# HORIZON_CONFIG's old per-bucket min_fundamental (55/60/55 for 3m/6m/1yr)
# now that trading_window_days can be any value, not just those 3 buckets.
STOCK_MIN_FUNDAMENTAL = 60


# ─────────────────────────────────────────────────────────────────────────────
# Stock Universe (watchlist + portfolio only — no hardcoded supplement)
# ─────────────────────────────────────────────────────────────────────────────

def get_stock_universe(user_id: str, watchlist_mode: str = "default_plus_mine") -> list[str]:
    """
    Delegates entirely to scanner.universe.get_scan_universe() — the
    single shared source of truth for the scan universe, already used
    by the options scanner. This used to be a second, parallel
    implementation with its own separate watchlist lookup (via
    watchlist_sync.get_db_watchlist) — folded into one to avoid
    exactly the kind of two-systems drift found elsewhere tonight
    (recommendations tables, the original watchlist duplication).
    """
    from app.scanner.universe import get_scan_universe
    return get_scan_universe(user_id=user_id, watchlist_mode=watchlist_mode)


# ─────────────────────────────────────────────────────────────────────────────
# Options Horizon Recommendation
# ─────────────────────────────────────────────────────────────────────────────

def get_options_for_horizon(
    ticker: str,
    horizon: str,
    budget: float,
    user_id: str | None = None,
    trading_window_days: int | None = None,
    stop_loss_pct: float | None = None,
    profit_target_pct: float | None = None,
) -> dict:
    """
    Build an options recommendation for a target trading window.

    trading_window_days/stop_loss_pct/profit_target_pct are real user
    inputs, following the same pattern rescan_engine.py's options engine
    already uses — when omitted, trading_window_days falls back to the
    horizon bucket's DTE-range midpoint (so existing callers that only
    pass `horizon` keep working unchanged).
    """
    from app.strategy.engine import build_recommendation, _get_all_expiries
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package
    from app.market_data.uw_market_data import get_bars
    from app.technical_analysis.engine import get_technical_profile
    from app.utils.trade_windows import compute_target_date

    config = HORIZON_CONFIG.get(horizon, HORIZON_CONFIG["1m"])

    if trading_window_days is None:
        trading_window_days = (config["dte_min"] + config["dte_max"]) // 2

    from_date = (datetime.now()-timedelta(days=300)).strftime('%Y-%m-%d')
    to_date   = datetime.now().strftime('%Y-%m-%d')
    bars      = get_bars(ticker, 1, 'day', from_date, to_date)
    ta        = get_technical_profile(ticker, bars) if bars else {}
    signal    = score_signal_package(get_signal_package(ticker))

    # Nearest REAL listed expiry to the target date — same approach
    # rescan_engine.py uses for SPY/QQQ (nearest_friday_to), but against
    # this ticker's actual listed expiries instead of an assumed weekly
    # Friday cadence (most individual stocks don't have weekly options).
    target_expiry_date = compute_target_date(trading_window_days)
    all_exp = _get_all_expiries(ticker)
    if not all_exp:
        return {"error": f"No listed expiries found for {ticker}"}

    target_dt = datetime.strptime(target_expiry_date, "%Y-%m-%d")
    best_exp  = min(all_exp, key=lambda exp: abs((datetime.strptime(exp, "%Y-%m-%d") - target_dt).days))
    best_dte  = (datetime.strptime(best_exp, "%Y-%m-%d") - datetime.now()).days
    print(f"[Horizon] {ticker} {horizon}: target {target_expiry_date} "
          f"({trading_window_days}d window) -> nearest listed expiry {best_exp} ({best_dte} DTE)")

    rec = build_recommendation(
        ticker      = ticker,
        ta_profile  = ta,
        flow_signal = signal,
        budget      = budget,
        user_id     = user_id,
        min_dte     = max(best_dte - 1, 0),
        max_dte     = best_dte + 1,
    )

    if rec:
        rec["horizon"]             = horizon
        rec["horizon_label"]       = config["label"]
        rec["rec_type"]            = "options"
        rec["trading_window_days"] = trading_window_days

        # User-driven stop/target take priority over whatever R/R-derived
        # dollar target_profit/stop_loss the strategy engine computed —
        # mirrors rescan_engine.py's new-pick path
        # (trade["target_pct"] = profit_target_pct, trade["stop_pct"] = -stop_loss_pct).
        best = rec.get("best")
        if best is not None:
            if profit_target_pct is not None:
                best["target_pct"] = profit_target_pct
            if stop_loss_pct is not None:
                best["stop_pct"] = -stop_loss_pct
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# Stock Horizon Recommendation
# ─────────────────────────────────────────────────────────────────────────────

def get_stock_for_horizon(
    ticker: str,
    horizon: str,
    budget: float,
    current_price: float | None = None,
    trading_window_days: int | None = None,
    stop_loss_pct: float | None = None,
    profit_target_pct: float | None = None,
) -> dict:
    """
    Build a stock recommendation for a target trading window.

    trading_window_days/stop_loss_pct/profit_target_pct are real user
    inputs — when omitted, falls back to the horizon bucket's own
    STOCK_UPSIDE_TARGETS/stop_pct/momentum-lookback defaults, so existing
    callers that only pass `horizon` keep working unchanged.
    """
    from app.recommendations.fundamentals import (
        get_fundamentals, get_dp_accumulation_score, score_fundamentals,
        analyst_target_reliability,
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

    min_fund = STOCK_MIN_FUNDAMENTAL
    if fund_score["fundamental_score"] < min_fund:
        return {
            "filtered": True,
            "reason":   f"Fundamental score {fund_score['fundamental_score']}/100 below {min_fund} threshold",
            "ticker":   ticker,
            "horizon":  horizon,
        }

    momentum = _get_momentum(ticker, horizon, trading_window_days)

    # Discount the raw analyst mean target by the same reliability factor
    # smart_stock_scan.py uses for ranking (thin coverage below ~$15 and
    # wide analyst low/high disagreement both reduce trust) — otherwise a
    # single-analyst-outlier target inflates the shown target_price even
    # though it's already discounted out of the fundamental_score gate above.
    raw_analyst_target = fundamentals.get("target_mean_price")
    analyst_target      = raw_analyst_target
    reliability          = 1.0
    if raw_analyst_target and current_price > 0:
        reliability = analyst_target_reliability(
            current_price,
            fundamentals.get("target_low_price", 0) or 0,
            fundamentals.get("target_high_price", 0) or 0,
            raw_analyst_target,
        )
        analyst_target = current_price + (raw_analyst_target - current_price) * reliability

    if profit_target_pct is not None:
        momentum_target = current_price * (1 + profit_target_pct / 100)
    else:
        momentum_target = current_price * (1 + STOCK_UPSIDE_TARGETS.get(horizon, 0.25))

    if analyst_target and analyst_target > current_price:
        target_price = max(analyst_target, momentum_target)
        target_source = "analyst + momentum"
    else:
        target_price = momentum_target
        target_source = "momentum"

    target_pct = round((target_price - current_price) / current_price * 100, 1)

    stop_pct   = -stop_loss_pct if stop_loss_pct is not None else config["stop_pct"]
    stop_price = round(current_price * (1 + stop_pct / 100), 2)

    shares = int(budget / current_price)
    if shares < 1:
        shares = 1
    total_cost = round(shares * current_price, 2)

    thesis = _generate_stock_thesis(
        ticker, horizon, config, fundamentals, dp, fund_score,
        current_price, target_price, target_pct, momentum, stop_pct
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
        "trading_window_days": trading_window_days,
        "rec_type":          "stock",
        "direction":         "BULLISH",
        "fundamental_score": fund_score["fundamental_score"],
        "fundamental_breakdown": fund_score["breakdown"],
        "dp_score":          dp["score"],
        "dp_note":           dp["note"],
        "analyst_target":    round(analyst_target, 2) if analyst_target else analyst_target,
        "raw_analyst_target": raw_analyst_target,  # undiscounted, for transparency
        "analyst_reliability": reliability,
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


def _lookback_period_for_days(days: int) -> str:
    """Closest yfinance history(period=...) bucket for a day count.

    yfinance only accepts a fixed set of period strings (no arbitrary
    "Nd"), so this rounds trading_window_days to the nearest supported
    bucket rather than passing the raw day count straight through.
    """
    if days <= 7:   return "5d"
    if days <= 30:  return "1mo"
    if days <= 90:  return "3mo"
    if days <= 180: return "6mo"
    if days <= 365: return "1y"
    return "2y"


def _get_momentum(ticker: str, horizon: str, trading_window_days: int | None = None) -> dict:
    try:
        import yfinance as yf
        if trading_window_days is not None:
            lookback = _lookback_period_for_days(trading_window_days)
        else:
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
    momentum: dict, stop_pct: float,
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
        f"stop ${current_price * (1 + stop_pct/100):.2f} ({stop_pct}%)."
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
    trading_window_days: int | None = None,
    stop_loss_pct: float | None = None,
    profit_target_pct: float | None = None,
) -> dict:
    """
    trading_window_days/stop_loss_pct/profit_target_pct pass straight
    through to get_options_for_horizon/get_stock_for_horizon - no new
    logic here beyond the existing options/stock/both routing. Omitted
    (None) preserves the old horizon-bucket-only behavior.
    """
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
        rec = get_options_for_horizon(
            ticker, horizon, budget, user_id,
            trading_window_days=trading_window_days,
            stop_loss_pct=stop_loss_pct, profit_target_pct=profit_target_pct,
        )
        result["options_rec"] = rec
        result["primary_rec"] = "options"

    elif rec_type == "stock":
        rec = get_stock_for_horizon(
            ticker, horizon, budget,
            trading_window_days=trading_window_days,
            stop_loss_pct=stop_loss_pct, profit_target_pct=profit_target_pct,
        )
        result["stock_rec"] = rec
        result["primary_rec"] = "stock"

    elif rec_type == "both":
        options_rec = get_options_for_horizon(
            ticker, horizon, budget, user_id,
            trading_window_days=trading_window_days,
            stop_loss_pct=stop_loss_pct, profit_target_pct=profit_target_pct,
        )
        stock_rec = get_stock_for_horizon(
            ticker, horizon, budget,
            trading_window_days=trading_window_days,
            stop_loss_pct=stop_loss_pct, profit_target_pct=profit_target_pct,
        )
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
    trading_window_days: int | None = None,
    stop_loss_pct: float | None = None,
    profit_target_pct: float | None = None,
) -> dict:
    """
    Scan the user's actual watchlist + portfolio for horizon recommendations.

    trading_window_days/stop_loss_pct/profit_target_pct just pass through
    to whichever of run_smart_stock_scan/get_horizon_recommendation this
    scans with per candidate - no new logic of its own.
    """
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

    # Pure stock horizons (6m/1yr) delegate to smart_stock_scan.py's
    # composite fundamentals+velocity+insider pre-filter instead of a naive
    # per-ticker loop — the same engine the web dashboard's stock scan
    # already uses (previously only reachable from the web, never MCP), so
    # get_scan_universe-wide stock picks are the same quality regardless of
    # channel. "options"/"both" horizons are unaffected — options selection
    # and 3m's combined options+stock loop are unchanged.
    if rec_type == "stock" and user_id:
        from app.recommendations.smart_stock_scan import run_smart_stock_scan

        market_open   = _is_market_open()
        next_day_flag = not market_open
        scan_result   = run_smart_stock_scan(
            user_id, horizon=horizon, budget=budget, top_n=top_n, tickers=tickers,
            trading_window_days=trading_window_days,
            stop_loss_pct=stop_loss_pct, profit_target_pct=profit_target_pct,
        )
        results = [
            {
                "ticker":       stock_rec.get("ticker", ""),
                "horizon":      horizon,
                "label":        config["label"],
                "description":  config["description"],
                "market_open":  market_open,
                "next_day":     next_day_flag,
                "stock_rec":    stock_rec,
                "primary_rec":  "stock",
            }
            for stock_rec in scan_result.get("stocks", [])
        ]
        elapsed = round(time.time()-t0, 1)
        print(f"[HorizonScan] Done in {elapsed}s — {len(results)} passed "
              f"(smart_stock_scan, {scan_result.get('scored',0)} scored)")
        return {
            "horizon":         horizon,
            "label":           config["label"],
            "recommendations": results[:top_n],
            "filtered_count":  max(scan_result.get("scored", 0) - len(results), 0),
            "total_scanned":   len(tickers),
            "elapsed":         elapsed,
            "date":            date.today().isoformat(),
        }

    results  = []
    filtered = []

    for ticker in tickers:
        try:
            rec = get_horizon_recommendation(
                ticker, horizon, budget, user_id,
                trading_window_days=trading_window_days,
                stop_loss_pct=stop_loss_pct, profit_target_pct=profit_target_pct,
            )

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
