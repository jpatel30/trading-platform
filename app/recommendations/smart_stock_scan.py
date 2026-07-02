"""
Smart Stock Scanner — Predictive scoring across all watchlist tickers.

Scoring weights:
  Fundamentals:  50% (analyst upside, PEG, revenue growth, margins)
  Velocity:      25% (signal acceleration over 3 days)
  Insider:       25% (SEC Form 4 purchases/sales)

Two phases:
  Phase 1: Lightweight parallel scoring — all 129 tickers (~30s)
  Phase 2: Deep analysis on top 10 — full thesis + entry/stop/target (~50s)

Target: completes within 90s
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


def _score_fundamentals(ticker: str, price: float) -> dict:
    """
    Fast fundamental pre-screen using yfinance.
    Returns score 0-100 and key metrics.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # Use fast_info first (no network call if cached)
        fi = t.fast_info

        # Phase 1: lightweight only — skip slow .info call
        # Full fundamentals run in Phase 2 via get_stock_for_horizon
        try:
            apt = t.analyst_price_targets  # fast, cached
            target_mean = float(apt.get("mean", 0) or 0) if isinstance(apt, dict) else 0
        except Exception:
            target_mean = 0
        peg = 0; revenue_growth = 0; profit_margin = 0; analyst_count = 0; rec_mean = 3.0

        if not price or price <= 0:
            return {"score": 0, "filtered": True, "reason": "no price"}

        # Upside to analyst target (40% of fund score)
        upside_pct = ((target_mean - price) / price * 100) if target_mean and price else 0
        if upside_pct < 5:
            return {"score": 0, "filtered": True, "reason": f"low upside {upside_pct:.1f}%"}

        upside_score = min(upside_pct / 50 * 100, 100)  # 50% upside = 100 score

        # PEG ratio (30% of fund score) — lower is better for growth
        if peg and 0 < peg < 3:
            peg_score = max(0, (3 - peg) / 3 * 100)  # PEG=0 → 100, PEG=3 → 0
        else:
            peg_score = 40  # neutral if no PEG

        # Revenue growth (20% of fund score)
        rev_score = min(max(revenue_growth * 100, 0), 100) if revenue_growth else 30

        # Analyst consensus (10% of fund score) — 1=strong buy, 5=sell
        analyst_score = max(0, (5 - rec_mean) / 4 * 100) if rec_mean else 50

        # Combined fundamental score (0-100)
        fund_score = (
            upside_score  * 0.40 +
            peg_score     * 0.30 +
            rev_score     * 0.20 +
            analyst_score * 0.10
        )

        return {
            "score":          round(fund_score, 1),
            "upside_pct":     round(upside_pct, 1),
            "target_price":   round(target_mean, 2),
            "peg":            round(peg, 2),
            "revenue_growth": round(revenue_growth, 3),
            "profit_margin":  round(profit_margin, 3),
            "analyst_count":  analyst_count,
            "rec_mean":       rec_mean,
            "filtered":       False,
        }
    except Exception as e:
        return {"score": 0, "filtered": True, "reason": str(e)[:50]}


def _score_velocity(ticker: str, user_id: str,
                    velocity_cache: dict | None = None) -> dict:
    """
    Velocity score from signal_history.
    Falls back to real-time UW flow if no history.
    Returns score 0-100 (50 = neutral).
    """
    if velocity_cache and ticker in velocity_cache:
        v = velocity_cache[ticker]
        vel = v.get("velocity", 0)
        # Convert velocity to 0-100 score (0=strongly fading, 50=neutral, 100=strongly accelerating)
        score = min(max(50 + vel * 0.5, 0), 100)
        return {
            "score":       round(score, 1),
            "velocity":    vel,
            "direction":   v.get("direction", "STABLE"),
            "days_data":   v.get("days_data", 0),
            "source":      "history",
        }

    # No history — real-time UW fetch
    try:
        from app.options_flow.unusual_whales import get_flow_alerts, get_dark_pool_ticker
        flow = get_flow_alerts(ticker=ticker, limit=20) or []
        dp   = get_dark_pool_ticker(ticker, limit=20) or []

        bull = sum(1 for a in flow if a.get("sentiment") in ("BULLISH","CALL"))
        bear = sum(1 for a in flow if a.get("sentiment") in ("BEARISH","PUT"))
        tot  = bull + bear
        flow_score = (bull - bear) / tot * 100 if tot else 0

        dp_buy  = sum(1 for d in dp if d.get("side") in ("BUY","A"))
        dp_sell = sum(1 for d in dp if d.get("side") in ("SELL","B"))
        dp_tot  = dp_buy + dp_sell
        dp_score = (dp_buy - dp_sell) / dp_tot * 100 if dp_tot else 0

        combined = flow_score * 0.6 + dp_score * 0.4
        score    = min(max(50 + combined * 0.5, 0), 100)

        return {
            "score":     round(score, 1),
            "velocity":  round(combined, 1),
            "direction": "BULLISH" if combined > 20 else "BEARISH" if combined < -20 else "NEUTRAL",
            "days_data": 0,
            "source":    "realtime",
        }
    except Exception:
        return {"score": 50, "velocity": 0, "direction": "NEUTRAL", "source": "unavailable"}


def _score_insider(ticker: str) -> dict:
    """
    Insider score from SEC EDGAR Form 4.
    Returns score 0-100 (50 = no activity).
    """
    try:
        from app.signals.edgar_insider import get_insider_activity
        data   = get_insider_activity(ticker, days=5)
        signal = data.get("signal", "NEUTRAL")

        if signal == "STRONG_BULLISH":
            score = 95
        elif signal == "BULLISH":
            score = 75
        elif signal == "NEUTRAL":
            score = 50
        elif signal == "BEARISH":
            score = 20
        else:
            score = 50

        return {
            "score":       score,
            "signal":      signal,
            "buy_value":   data.get("buy_value", 0),
            "sell_value":  data.get("sell_value", 0),
            "csuite_buy":  data.get("csuite_buy", False),
            "csuite_sell": data.get("csuite_sell", False),
        }
    except Exception:
        return {"score": 50, "signal": "NEUTRAL"}


def run_smart_stock_scan(
    user_id:    str,
    horizon:    str   = "6m",
    budget:     float = 5000.0,
    top_n:      int   = 5,
) -> dict:
    """
    Full predictive stock scan across all watchlist tickers.
    Phase 1: Parallel lightweight scoring — all tickers
    Phase 2: Deep analysis on top 10
    """
    from app.scanner.universe import get_scan_universe
    from app.options_flow.unusual_whales import get_stock_state
    from app.signals.velocity_tracker import get_velocity_scores

    t_start = time.time()
    tickers = get_scan_universe(user_id=user_id)
    print(f"[StockScan] Scanning {len(tickers)} tickers for {horizon} horizon")

    # Pre-fetch velocity for all tickers (single DB query)
    velocity_cache = {}
    try:
        velocity_cache = get_velocity_scores(tickers, user_id)
        print(f"[StockScan] Velocity data: {len(velocity_cache)} tickers with history")
    except Exception as e:
        print(f"[StockScan] Velocity cache failed: {e}")

    # Fetch prices + fast_data for all tickers in parallel (yfinance fast_info)
    prices    = {}
    fast_data = {}

    def _fetch_fast(ticker):
        try:
            fi = yf.Ticker(ticker).fast_info
            p  = fi.last_price or 0
            pc = fi.previous_close or p
            return ticker, {
                "price":      round(p, 2),
                "prev_close": round(pc, 2),
                "change_pct": round((p - pc) / pc * 100, 2) if pc else 0,
                "volume":     fi.last_volume or 0,
                "market_cap": fi.market_cap or 0,
                "52w_high":   fi.year_high or 0,
                "52w_low":    fi.year_low or 0,
                "50d_avg":    fi.fifty_day_average or 0,
                "200d_avg":   fi.two_hundred_day_average or 0,
                "year_chg":   round((fi.year_change or 0) * 100, 1),
            }
        except Exception:
            return ticker, {"price": 0}

    t_price = time.time()
    with ThreadPoolExecutor(max_workers=20) as ex:
        for ticker, data in ex.map(_fetch_fast, tickers):
            fast_data[ticker] = data
            prices[ticker]    = data.get("price", 0)
    print(f"[StockScan] Prices fetched in {time.time()-t_price:.1f}s")

    # Phase 1: Parallel scoring of all tickers
    def _score_ticker(ticker: str) -> dict:
        fd    = fast_data.get(ticker, {})
        price = fd.get("price", 0)
        if not price:
            return {"ticker": ticker, "composite": 0, "filtered": True}

        # Skip very low volume tickers
        if fd.get("volume", 0) < 50_000:
            return {"ticker": ticker, "composite": 0, "filtered": True, "reason": "low_volume"}

        fund = _score_fundamentals(ticker, price)
        velocity = _score_velocity(ticker, user_id, velocity_cache)
        insider  = _score_insider(ticker)

        if fund.get("filtered"):
            return {"ticker": ticker, "composite": 0, "filtered": True,
                    "reason": fund.get("reason","")}

        # Composite score: Fund 50%, Velocity 25%, Insider 25%
        composite = (
            fund["score"]     * 0.50 +
            velocity["score"] * 0.25 +
            insider["score"]  * 0.25
        )

        return {
            "ticker":          ticker,
            "price":           price,
            "composite":       round(composite, 1),
            "fund_score":      fund["score"],
            "velocity_score":  velocity["score"],
            "insider_score":   insider["score"],
            "upside_pct":      fund.get("upside_pct", 0),
            "target_price":    fund.get("target_price", 0),
            "peg":             fund.get("peg", 0),
            "revenue_growth":  fund.get("revenue_growth", 0),
            "velocity":        velocity.get("velocity", 0),
            "volume":         fd.get("volume", 0),
            "change_pct_today": fd.get("change_pct", 0),
            "above_50d":      price > fd.get("50d_avg", price),
            "near_52w_high":  price >= fd.get("52w_high", price) * 0.90,
            "year_chg":       fd.get("year_chg", 0),
            "volume":         fd.get("volume", 0),
            "change_pct_today": fd.get("change_pct", 0),
            "above_50d":      price > fd.get("50d_avg", price),
            "near_52w_high":  price >= fd.get("52w_high", price) * 0.90,
            "year_chg":       fd.get("year_chg", 0),
            "velocity_dir":    velocity.get("direction", "NEUTRAL"),
            "insider_signal":  insider.get("signal", "NEUTRAL"),
            "csuite_buy":      insider.get("csuite_buy", False),
            "analyst_count":   fund.get("analyst_count", 0),
            "filtered":        False,
        }

    print(f"[StockScan] Phase 1: scoring {len(tickers)} tickers in parallel...")
    candidates = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_score_ticker, t): t for t in tickers}
        for fut in as_completed(futures, timeout=120):
            try:
                result = fut.result()
                if not result.get("filtered") and result.get("composite", 0) > 0:
                    candidates.append(result)
            except Exception:
                pass

    # Sort by composite score
    candidates.sort(key=lambda x: x["composite"], reverse=True)
    phase1_time = time.time() - t_start
    print(f"[StockScan] Phase 1 done in {phase1_time:.1f}s — {len(candidates)} candidates")

    if not candidates:
        return {"stocks": [], "source": "stock_scan", "elapsed": phase1_time}

    # Phase 2: Deep analysis on top 10
    print(f"[StockScan] Phase 2: deep analysis on top {min(10, len(candidates))} candidates...")
    from app.recommendations.horizon_engine import get_stock_for_horizon

    results   = []
    seen      = set()
    for cand in candidates[:10]:
        ticker = cand["ticker"]
        if ticker in seen:
            continue
        seen.add(ticker)
        try:
            rec = get_stock_for_horizon(ticker, horizon, budget,
                                        current_price=cand["price"])
            if rec and not rec.get("filtered"):
                # Merge predictive signals into recommendation
                rec["composite_score"]  = cand["composite"]
                rec["velocity"]         = cand["velocity"]
                rec["velocity_dir"]     = cand["velocity_dir"]
                rec["insider_signal"]   = cand["insider_signal"]
                rec["csuite_buy"]       = cand["csuite_buy"]
                rec["velocity_score"]   = cand["velocity_score"]
                rec["insider_score"]    = cand["insider_score"]
                rec["status"]           = "NEW"

                # Boost fundamental_score with composite
                rec["fundamental_score"] = round(
                    rec.get("fundamental_score", 0) * 0.7 + cand["composite"] * 0.3
                )
                results.append(rec)

            if len(results) >= top_n:
                break
        except Exception as e:
            print(f"[StockScan] Deep analysis failed for {ticker}: {e}")

    total_time = round(time.time() - t_start, 1)
    print(f"[StockScan] COMPLETE in {total_time}s — {len(results)} picks")

    return {
        "stocks":    results,
        "source":    "smart_stock_scan",
        "elapsed":   total_time,
        "scored":    len(candidates),
    }
