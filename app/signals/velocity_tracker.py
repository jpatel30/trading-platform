"""
Signal Velocity Tracker.

Two modes:
  1. Daily snapshot (4:15 PM ET) — stores all watchlist ticker signals
  2. Real-time velocity (during scan) — compares today vs 3-day avg

Velocity formula:
  velocity = (today_score - avg_3day) / max(abs(avg_3day), 1) * 100
  +50% = signal accelerating (institutions positioning)
  -50% = signal fading (unwind)

Auto-switches from 3-day to 5-day lookback once 5 days of data exist.
"""
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed


def save_daily_signals(user_id: str) -> dict:
    """
    Snapshot all watchlist ticker signals at market close.
    Run at 4:15 PM ET daily.
    Returns: count of tickers saved.
    """
    from sqlalchemy import text
    from app.db.session import get_session
    from app.scanner.universe import get_scan_universe
    from app.options_flow.unusual_whales import (
        get_flow_alerts, get_dark_pool_recent, get_iv_rank,
        get_stock_state,
    )
    from app.signals.edgar_insider import get_insider_activity

    print(f"[Velocity] Starting daily signal snapshot...")
    tickers = get_scan_universe(user_id=user_id)

    # Batch calls first (most efficient)
    all_flow = get_flow_alerts(limit=500) or []
    all_dp   = get_dark_pool_recent(limit=200) or []

    # Group by ticker
    flow_by = {}
    for a in all_flow:
        flow_by.setdefault(a.get("ticker",""), []).append(a)
    dp_by = {}
    for d in all_dp:
        dp_by.setdefault(d.get("ticker",""), []).append(d)

    # Per-ticker enrichment (parallel, token bucket handles rate)
    def _enrich(ticker: str) -> dict:
        try:
            # Use only batch data — no per-ticker UW calls in snapshot
            # (per-ticker calls compete with token bucket and timeout)
            tf = flow_by.get(ticker, [])
            td = dp_by.get(ticker, [])

            # Price from yfinance (free, no rate limit)
            try:
                import yfinance as _yf
                fi = _yf.Ticker(ticker).fast_info
                state = {"price": fi.last_price or 0, "change_pct": 0}
                if fi.previous_close and fi.last_price:
                    state["change_pct"] = round((fi.last_price - fi.previous_close) / fi.previous_close * 100, 2)
            except Exception:
                state = {}

            iv      = {}
            insider = {"signal": "NEUTRAL", "has_buy": False, "has_sell": False, "total_value": 0}

            bull = sum(1 for a in tf if a.get("type","").lower()=="call" or a.get("sentiment","").upper() in ("BULLISH","CALL"))
            bear = sum(1 for a in tf if a.get("type","").lower()=="put" or a.get("sentiment","").upper() in ("BEARISH","PUT"))
            tot  = bull + bear
            flow_score = round((bull-bear)/tot*100, 1) if tot else 0

            dp_buy  = sum(1 for d in td if d.get("side") in ("BUY","A"))
            dp_sell = sum(1 for d in td if d.get("side") in ("SELL","B"))
            dp_tot  = dp_buy + dp_sell
            dp_score = round((dp_buy-dp_sell)/dp_tot*100, 1) if dp_tot else 0

            call_vol = sum(a.get("volume",0) for a in tf if a.get("sentiment") in ("BULLISH","CALL"))
            put_vol  = sum(a.get("volume",0) for a in tf if a.get("sentiment") in ("BEARISH","PUT"))
            cpr      = round(call_vol/put_vol, 2) if put_vol else None

            gex_val = 0  # GEX not fetched in snapshot

            return {
                "ticker":       ticker,
                "flow_score":   flow_score,
                "flow_alerts":  len(tf),
                "sweep_count":  sum(1 for a in tf if a.get("is_sweep")),
                "call_vol":     call_vol,
                "put_vol":      put_vol,
                "call_put_ratio": cpr,
                "dp_score":     dp_score,
                "dp_prints":    len(td),
                "iv_rank":      float(iv.get("iv_rank", 0) or 0),
                "gex_score":    float(gex_val),
                "insider_buy":  insider.get("has_buy", False),
                "insider_sell": insider.get("has_sell", False),
                "insider_value": insider.get("total_value", 0),
                "price":        float(state.get("price", 0) or 0),
                "change_pct":   float(state.get("change_pct", 0) or 0),
            }
        except Exception as e:
            print(f"[Velocity] Enrich failed for {ticker}: {e}")
            return {"ticker": ticker}

    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_enrich, t): t for t in tickers}
        for fut in as_completed(futures, timeout=180):
            try:
                results.append(fut.result())
            except Exception:
                pass

    # Store to DB
    saved = 0
    for r in results:
        if not r.get("price"):
            continue
        try:
            with get_session() as s:
                s.execute(text("""
                    INSERT INTO signal_history
                        (user_id, ticker, date, flow_score, flow_alerts,
                         sweep_count, call_vol, put_vol, call_put_ratio,
                         dp_score, dp_prints, iv_rank, gex_score,
                         insider_buy, insider_sell, insider_value,
                         price, change_pct)
                    VALUES
                        (:uid, :ticker, CURRENT_DATE, :flow_score, :flow_alerts,
                         :sweep_count, :call_vol, :put_vol, :call_put_ratio,
                         :dp_score, :dp_prints, :iv_rank, :gex_score,
                         :insider_buy, :insider_sell, :insider_value,
                         :price, :change_pct)
                    ON CONFLICT (user_id, ticker, date) DO UPDATE SET
                        flow_score   = EXCLUDED.flow_score,
                        flow_alerts  = EXCLUDED.flow_alerts,
                        sweep_count  = EXCLUDED.sweep_count,
                        dp_score     = EXCLUDED.dp_score,
                        iv_rank      = EXCLUDED.iv_rank,
                        gex_score    = EXCLUDED.gex_score,
                        insider_buy  = EXCLUDED.insider_buy,
                        insider_sell = EXCLUDED.insider_sell,
                        insider_value= EXCLUDED.insider_value,
                        price        = EXCLUDED.price,
                        change_pct   = EXCLUDED.change_pct
                """), {**r, "uid": user_id,
                       "flow_alerts": r.get("flow_alerts",0),
                       "sweep_count": r.get("sweep_count",0),
                       "call_vol":    r.get("call_vol",0),
                       "put_vol":     r.get("put_vol",0),
                       "call_put_ratio": r.get("call_put_ratio"),
                       "dp_prints":   r.get("dp_prints",0),
                       "insider_buy": r.get("insider_buy",False),
                       "insider_sell":r.get("insider_sell",False),
                       "insider_value":r.get("insider_value",0)})
            saved += 1
        except Exception as e:
            print(f"[Velocity] DB store failed for {r.get('ticker')}: {e}")

    print(f"[Velocity] Daily snapshot complete: {saved}/{len(tickers)} tickers saved")
    return {"saved": saved, "total": len(tickers)}


def get_velocity_scores(tickers: list[str], user_id: str) -> dict[str, dict]:
    """
    Real-time velocity scoring during scan.
    Compares today's signals vs 3-day (or 5-day) historical average.
    Returns dict: {ticker: {velocity, direction, days_data, insider_signal}}
    """
    from sqlalchemy import text
    from app.db.session import get_session

    if not tickers:
        return {}

    try:
        with get_session() as s:
            rows = s.execute(text("""
                SELECT ticker,
                       date,
                       flow_score,
                       dp_score,
                       iv_rank,
                       gex_score,
                       insider_buy,
                       insider_sell,
                       insider_value,
                       sweep_count,
                       call_put_ratio,
                       change_pct
                FROM signal_history
                WHERE user_id = :uid
                  AND ticker  = ANY(:tickers)
                  AND date   >= CURRENT_DATE - 7
                ORDER BY ticker, date DESC
            """), {"uid": user_id, "tickers": list(tickers)}).fetchall()
    except Exception:
        return {}

    # Group by ticker
    by_ticker: dict = {}
    for r in rows:
        by_ticker.setdefault(r.ticker, []).append(r)

    result = {}
    for ticker, history in by_ticker.items():
        # Use 5-day lookback if we have 5+ days, else 3-day
        lookback = 5 if len(history) >= 5 else 3
        recent   = history[:lookback]

        if len(recent) < 2:
            # Not enough history — still check insider signal
            ins = history[0] if history else None
            result[ticker] = {
                "velocity":       0,
                "direction":      "NEUTRAL",
                "days_data":      len(history),
                "insider_buy":    ins.insider_buy if ins else False,
                "insider_sell":   ins.insider_sell if ins else False,
                "insider_value":  float(ins.insider_value or 0) if ins else 0,
                "gex_score":      0,
                "sweep_trend":    0,
            }
            continue

        # Flow velocity — most recent vs average of prior days
        today_flow  = float(recent[0].flow_score or 0)
        prior_flows = [float(r.flow_score or 0) for r in recent[1:]]
        avg_flow    = sum(prior_flows) / len(prior_flows) if prior_flows else 0
        flow_vel    = (today_flow - avg_flow) / max(abs(avg_flow), 1) * 100

        # DP velocity
        today_dp  = float(recent[0].dp_score or 0)
        prior_dps = [float(r.dp_score or 0) for r in recent[1:]]
        avg_dp    = sum(prior_dps) / len(prior_dps) if prior_dps else 0
        dp_vel    = (today_dp - avg_dp) / max(abs(avg_dp), 1) * 100

        # Combined velocity (flow weighted 60%, dp 40%)
        velocity = round(flow_vel * 0.6 + dp_vel * 0.4, 1)

        # Direction from velocity
        if velocity > 30:
            direction = "ACCELERATING_BULLISH"
        elif velocity < -30:
            direction = "ACCELERATING_BEARISH"
        elif velocity > 10:
            direction = "BUILDING_BULLISH"
        elif velocity < -10:
            direction = "FADING"
        else:
            direction = "STABLE"

        # Sweep trend (count rising = institutional positioning)
        sweep_counts = [int(r.sweep_count or 0) for r in recent]
        sweep_trend  = sweep_counts[0] - (sum(sweep_counts[1:]) / max(len(sweep_counts[1:]),1))

        result[ticker] = {
            "velocity":       velocity,
            "direction":      direction,
            "days_data":      len(history),
            "lookback":       lookback,
            "flow_today":     today_flow,
            "flow_avg":       round(avg_flow, 1),
            "dp_today":       today_dp,
            "dp_avg":         round(avg_dp, 1),
            "gex_score":      float(recent[0].gex_score or 0),
            "insider_buy":    recent[0].insider_buy or False,
            "insider_sell":   recent[0].insider_sell or False,
            "insider_value":  float(recent[0].insider_value or 0),
            "sweep_trend":    round(sweep_trend, 1),
            "call_put_ratio": float(recent[0].call_put_ratio or 1),
        }

    return result


def apply_velocity_to_picks(picks: list[dict], velocity: dict[str, dict]) -> list[dict]:
    """
    Apply velocity multipliers to scanner picks.
    Modifies score in-place, adds velocity metadata.
    """
    for pick in picks:
        ticker = pick.get("ticker","")
        v = velocity.get(ticker)
        if not v:
            continue

        score = pick.get("score", 0)

        # Velocity multiplier
        if v["velocity"] > 50:
            multiplier = 1.8   # strongly accelerating
        elif v["velocity"] > 30:
            multiplier = 1.5   # accelerating
        elif v["velocity"] > 10:
            multiplier = 1.2   # building
        elif v["velocity"] < -30:
            multiplier = 0.7   # fading
        else:
            multiplier = 1.0

        # Insider bonus
        if v.get("insider_buy") and v.get("insider_value",0) > 100000:
            score += 0.3  # significant insider buying
        if v.get("insider_sell") and v.get("insider_value",0) > 500000:
            score -= 0.2  # large insider selling

        # Sweep trend bonus (3+ sweeps rising = institutional positioning)
        if v.get("sweep_trend",0) > 2:
            score += 0.2

        pick["score"]          = round(score * multiplier, 3)
        pick["velocity"]       = v["velocity"]
        pick["velocity_dir"]   = v["direction"]
        pick["insider_buy"]    = v.get("insider_buy", False)
        pick["insider_sell"]   = v.get("insider_sell", False)
        pick["gex_score"]      = v.get("gex_score", 0)
        pick["days_tracked"]   = v.get("days_data", 0)

    return picks


def schedule_daily_snapshot():
    """
    Start background thread that triggers daily snapshot at 4:15 PM ET.
    Called once at server startup.
    """
    import threading

    def _runner():
        from datetime import datetime, timezone
        import pytz
        et = pytz.timezone("America/New_York")

        while True:
            now_et = datetime.now(et)
            # Target: 4:15 PM ET on weekdays
            if now_et.weekday() < 5:  # Mon-Fri
                target = now_et.replace(hour=16, minute=15, second=0, microsecond=0)
                if now_et < target:
                    wait = (target - now_et).total_seconds()
                    print(f"[Velocity] Daily snapshot scheduled in {wait/3600:.1f}h")
                    import time; time.sleep(wait)
                else:
                    # Already past 4:15 PM — wait until tomorrow
                    import time; time.sleep(3600)
                    continue

                # Run snapshot for all users
                try:
                    from sqlalchemy import text
                    from app.db.session import get_session
                    with get_session() as s:
                        users = s.execute(text("SELECT id FROM users WHERE is_active=TRUE")).fetchall()
                    for u in users:
                        save_daily_signals(str(u.id))
                except Exception as e:
                    print(f"[Velocity] Daily snapshot failed: {e}")
            else:
                # Weekend — sleep until Monday
                import time; time.sleep(3600)

    t = threading.Thread(target=_runner, daemon=True, name="velocity-snapshot")
    t.start()
    print("[Velocity] Daily snapshot scheduler started (fires 4:15 PM ET weekdays)")
