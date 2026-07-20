"""
Smart Stock Scanner — Predictive scoring across all watchlist tickers.

Architecture (no timeouts possible):
  Pre-fetch 1: yfinance fast_info x all tickers (parallel, instant)
  Pre-fetch 2: Analyst targets x all tickers (parallel, 3s timeout each)
  Pre-fetch 3: Velocity from DB (single query, instant)
  Pre-fetch 4: Velocity UW real-time for uncovered (parallel, token bucket)
  Pre-fetch 5: EDGAR insider x all tickers (parallel, 3s timeout each)
  Scoring: pure math, zero network, zero timeouts
  Phase 2: get_stock_for_horizon on top 10

Weights: Fundamentals 50%, Velocity 25%, Insider 25%

Rewritten July 2026 (second pass) — two fixes:

1. Analyst-upside reliability discount. Raw analyst-mean upside was
   taken at face value with zero discount for how reliable that mean
   actually is. Concrete evidence: EVTL showed +517% upside on 5
   analysts at a $1.62 share price — a mean target that thin and that
   close to zero is dominated by single-analyst-outlier risk, not a
   real consensus. Because upside_pct feeds fund_score almost linearly
   (capped only at 100+), this structurally favored cheap/thin-coverage
   tickers over better-covered, more reliable names — explaining why
   recommendations kept clustering under $30. Fix: discount upside_pct
   by a reliability factor built from price level (thin coverage below
   ~$15) and analyst low/high dispersion (wide disagreement = low
   confidence) — both already free from the same analyst_price_targets
   call, no added network cost. raw_upside_pct and reliability are
   still returned for transparency; only the SCORING input changes.

2. Progress checkpoints (set_scan_status) at each phase boundary, so
   the UI progress bar shows real stages instead of sitting at 0% for
   the ~30s runtime (previously this function never called
   set_scan_status at all).

The reliability discount now lives in fundamentals.py
(analyst_target_reliability()), shared with get_stock_for_horizon
(horizon_engine.py) — originally this fixed only WHICH tickers reach
Phase 2 and their fund_score here, leaving the target_price shown to
the user (computed in horizon_engine.py, a separate file) undiscounted;
both now use the same function.
"""
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.signals.flow_scoring import compute_flow_score, compute_dp_score
from app.utils.scan_status import set_scan_status
from app.recommendations.fundamentals import analyst_target_reliability

logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def _fetch_fast_info(ticker: str) -> dict:
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        p  = fi.last_price or 0
        pc = fi.previous_close or p
        return {
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
            "quote_type": getattr(fi, "quote_type", "EQUITY"),
        }
    except Exception:
        return {"price": 0, "quote_type": "EQUITY"}


def _fetch_analyst_target(ticker: str) -> dict:
    """
    Returns mean/low/high — not just mean — so scoring can discount
    for analyst disagreement (dispersion), using data already fetched
    in this one call, no extra network cost.
    """
    try:
        import yfinance as yf
        apt = yf.Ticker(ticker).analyst_price_targets
        if isinstance(apt, dict):
            return {
                "mean": float(apt.get("mean", 0) or 0),
                "low":  float(apt.get("low", 0) or 0),
                "high": float(apt.get("high", 0) or 0),
            }
    except Exception:
        pass
    return {"mean": 0, "low": 0, "high": 0}


def _fetch_insider(ticker: str) -> dict:
    try:
        from app.signals.edgar_insider import get_insider_activity
        data   = get_insider_activity(ticker, days=5)
        signal = data.get("signal", "NEUTRAL")
        score  = {"STRONG_BULLISH": 95, "BULLISH": 75, "NEUTRAL": 50, "BEARISH": 20}.get(signal, 50)
        return {"score": score, "signal": signal,
                "csuite_buy": data.get("csuite_buy", False), "buy_value": data.get("buy_value", 0)}
    except Exception:
        return {"score": 50, "signal": "NEUTRAL"}


def _fetch_velocity_realtime(ticker: str) -> dict:
    try:
        from app.options_flow.unusual_whales import get_flow_alerts, get_dark_pool_ticker
        flow = get_flow_alerts(ticker=ticker, limit=20) or []
        dp   = get_dark_pool_ticker(ticker, limit=20) or []
        fs  = compute_flow_score(flow)["flow_score"]
        dps = compute_dp_score(dp)["dp_score"]
        combined = fs * 0.6 + dps * 0.4
        score    = min(max(50 + combined * 0.5, 0), 100)
        return {
            "score": round(score, 1), "velocity": round(combined, 1),
            "direction": "BULLISH" if combined > 20 else "BEARISH" if combined < -20 else "NEUTRAL",
        }
    except Exception:
        return {"score": 50, "velocity": 0, "direction": "NEUTRAL"}


def _score_ticker(ticker, fd, target, velocity, insider) -> dict:
    """Pure math — zero network calls, zero timeouts."""
    price = fd.get("price", 0)
    if not price:
        return {"ticker": ticker, "composite": 0, "filtered": True, "reason": "no price"}
    if fd.get("volume", 0) < 50_000:
        return {"ticker": ticker, "composite": 0, "filtered": True, "reason": "low_volume"}

    qt = fd.get("quote_type", "EQUITY")
    raw_upside_pct = 0.0
    reliability     = 1.0

    if qt not in ("EQUITY", ""):
        avg_50   = fd.get("50d_avg", price) or price
        hi_52w   = fd.get("52w_high", price) or price
        yr_chg   = fd.get("year_chg", 0)
        above_50 = 20 if price > avg_50 else 0
        yr_trend = min(max(yr_chg / 50 * 20, -20), 20)
        near_hi  = 20 if price >= hi_52w * 0.90 else 0
        fund_score = 30 + above_50 + yr_trend + near_hi
        upside_pct = 0
    else:
        mean = target.get("mean", 0) if isinstance(target, dict) else float(target or 0)
        if not mean:
            fund_score = 40
            upside_pct = 0
        else:
            raw_upside_pct = (mean - price) / price * 100
            if raw_upside_pct < -10:
                return {"ticker": ticker, "composite": 0, "filtered": True,
                        "reason": f"overvalued {raw_upside_pct:.1f}%"}
            low  = target.get("low", 0)  if isinstance(target, dict) else 0
            high = target.get("high", 0) if isinstance(target, dict) else 0
            reliability = analyst_target_reliability(price, low, high, mean)
            upside_pct  = raw_upside_pct * reliability  # discounted — this is what scores
            upside_score = min(max(upside_pct, 0), 100)
            fund_score   = upside_score*0.40 + 40*0.30 + 30*0.20 + 50*0.10

    composite = fund_score*0.50 + velocity.get("score",50)*0.25 + insider.get("score",50)*0.25

    return {
        "ticker": ticker, "price": price, "composite": round(composite,1),
        "fund_score": round(fund_score,1), "velocity_score": velocity.get("score",50),
        "insider_score": insider.get("score",50),
        "upside_pct": round(upside_pct,1),          # reliability-discounted (used for scoring)
        "raw_upside_pct": round(raw_upside_pct,1),  # undiscounted, for transparency/debugging
        "reliability": reliability,
        "target_price": round(target.get("mean",0) if isinstance(target, dict) else float(target or 0), 2),
        "velocity": velocity.get("velocity",0),
        "velocity_dir": velocity.get("direction","NEUTRAL"),
        "insider_signal": insider.get("signal","NEUTRAL"), "csuite_buy": insider.get("csuite_buy",False),
        "volume": fd.get("volume",0), "change_pct": fd.get("change_pct",0),
        "year_chg": fd.get("year_chg",0), "quote_type": qt, "filtered": False,
    }


def run_smart_stock_scan(user_id, horizon="6m", budget=5000.0, top_n=5,
                          watchlist_mode="default_plus_mine", tickers=None):
    """
    tickers: optional explicit universe override. A caller that already
    resolved the watchlist (e.g. horizon_engine.scan_for_horizon, which
    needs the same tickers for its options half too) can pass it directly
    instead of forcing a second get_scan_universe() lookup. Default None
    preserves the original behavior of resolving it here.
    """
    from app.signals.velocity_tracker import get_velocity_scores

    t_start = time.time()
    set_scan_status(user_id, "queued")

    if tickers is None:
        from app.scanner.universe import get_scan_universe
        tickers = get_scan_universe(user_id=user_id, watchlist_mode=watchlist_mode)
    print(f"[StockScan] {len(tickers)} tickers | {horizon} | ${budget:,.0f}")

    set_scan_status(user_id, "fundamentals")
    t0 = time.time()
    fast_data = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        for ticker, data in zip(tickers, ex.map(_fetch_fast_info, tickers)):
            fast_data[ticker] = data
    print(f"[StockScan] fast_info done in {time.time()-t0:.1f}s")

    set_scan_status(user_id, "analyst")
    t0 = time.time()
    analyst_targets = {}
    equities = [t for t in tickers if fast_data.get(t,{}).get("quote_type","EQUITY") in ("EQUITY","")]
    with ThreadPoolExecutor(max_workers=15) as ex:
        futures = {ex.submit(_fetch_analyst_target, t): t for t in equities}
        for fut in as_completed(futures, timeout=60):
            try:
                analyst_targets[futures[fut]] = fut.result()
            except Exception:
                pass
    print(f"[StockScan] Analyst targets: {len(analyst_targets)} in {time.time()-t0:.1f}s")

    set_scan_status(user_id, "signals")
    t0 = time.time()
    velocity_cache = {}
    try:
        velocity_cache = get_velocity_scores(tickers, user_id)
        print(f"[StockScan] Velocity DB: {len(velocity_cache)} in {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"[StockScan] Velocity DB failed: {e}")

    uncovered = [t for t in tickers if t not in velocity_cache][:20]
    if uncovered:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(_fetch_velocity_realtime, t): t for t in uncovered}
            for fut in as_completed(futures, timeout=30):
                try: velocity_cache[futures[fut]] = fut.result()
                except Exception: pass
        print(f"[StockScan] Velocity realtime: {len(uncovered)} in {time.time()-t0:.1f}s")

    t0 = time.time()
    insider_cache = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch_insider, t): t for t in tickers}
        for fut in as_completed(futures, timeout=45):
            try: insider_cache[futures[fut]] = fut.result()
            except Exception: insider_cache[futures[fut]] = {"score": 50, "signal": "NEUTRAL"}
    print(f"[StockScan] EDGAR: {len(insider_cache)} in {time.time()-t0:.1f}s")

    try:
        from sqlalchemy import text
        from app.db.session import get_session
        saved = 0
        for ticker in tickers:
            vc = velocity_cache.get(ticker, {})
            fd = fast_data.get(ticker, {})
            if not fd.get("price"):
                continue
            ins = insider_cache.get(ticker, {})
            try:
                with get_session() as s:
                    s.execute(text("""
                        INSERT INTO signal_history
                            (user_id, ticker, date, flow_score, dp_score,
                             insider_buy, insider_sell, price, change_pct)
                        VALUES
                            (:uid, :t, CURRENT_DATE, :fs, :dps, :ib, :is_, :p, :cp)
                        ON CONFLICT (user_id, ticker, date) DO UPDATE SET
                            flow_score = EXCLUDED.flow_score,
                            price      = EXCLUDED.price,
                            change_pct = EXCLUDED.change_pct
                    """), {
                        "uid": user_id, "t": ticker, "fs": vc.get("velocity", 0), "dps": 0,
                        "ib": ins.get("signal","NEUTRAL") in ("BULLISH","STRONG_BULLISH"),
                        "is_": ins.get("signal","NEUTRAL") == "BEARISH",
                        "p": fd.get("price", 0), "cp": fd.get("change_pct", 0),
                    })
                saved += 1
            except Exception:
                pass
        print(f"[StockScan] Saved {saved} signals to history")
    except Exception as e:
        print(f"[StockScan] Signal save failed: {e}")

    set_scan_status(user_id, "scoring")
    t0 = time.time()
    candidates = []
    for ticker in tickers:
        r = _score_ticker(
            ticker=ticker, fd=fast_data.get(ticker, {}),
            target=analyst_targets.get(ticker, {"mean":0,"low":0,"high":0}),
            velocity=velocity_cache.get(ticker, {"score":50,"velocity":0,"direction":"NEUTRAL"}),
            insider=insider_cache.get(ticker, {"score":50,"signal":"NEUTRAL"}),
        )
        if not r.get("filtered") and r.get("composite",0) > 0:
            candidates.append(r)

    candidates.sort(key=lambda x: x["composite"], reverse=True)
    print(f"[StockScan] Phase 1: {len(candidates)} candidates in {time.time()-t0:.3f}s")
    print(f"[StockScan] Top 5: {[c['ticker'] for c in candidates[:5]]}")

    if not candidates:
        set_scan_status(user_id, "complete")
        return {"stocks":[], "source":"smart_stock_scan", "elapsed": round(time.time()-t_start,1), "scored":0}

    set_scan_status(user_id, "deep_analysis")
    print(f"[StockScan] Phase 2: deep analysis on top {min(10,len(candidates))}...")
    from app.recommendations.horizon_engine import get_stock_for_horizon

    # No ETF exclusion — full watchlist eligible, including SPY/QQQ/IWM/
    # SMH/SOXX etc. get_stock_for_horizon() may still score these lower
    # if fundamentals.py's PEG/margin/revenue-growth fields come back
    # empty for a fund — that's data availability, not a ticker filter.
    print(f"[StockScan] Phase 2: {len(candidates)} candidates (no ETF exclusion)")
    results, seen = [], set()
    for cand in candidates[:10]:
        ticker = cand["ticker"]
        if ticker in seen: continue
        seen.add(ticker)
        try:
            rec = get_stock_for_horizon(ticker, horizon, budget, current_price=cand["price"])
            if rec and not rec.get("filtered"):
                rec.update({
                    "composite_score": cand["composite"], "velocity": cand["velocity"],
                    "velocity_dir": cand["velocity_dir"], "insider_signal": cand["insider_signal"],
                    "csuite_buy": cand["csuite_buy"], "status": "NEW",
                    "reliability": cand.get("reliability", 1.0),
                    "raw_upside_pct": cand.get("raw_upside_pct", 0),
                    "fundamental_score": round(rec.get("fundamental_score",0)*0.7 + cand["composite"]*0.3),
                })
                results.append(rec)
            if len(results) >= top_n: break
        except Exception as e:
            print(f"[StockScan] {ticker} failed: {e}")

    total = round(time.time()-t_start, 1)
    print(f"[StockScan] COMPLETE in {total}s — {len(results)} picks")
    set_scan_status(user_id, "complete")
    return {"stocks": results, "source": "smart_stock_scan", "elapsed": total, "scored": len(candidates)}
