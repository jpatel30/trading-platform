"""
Two-Tier Convergence Scanner (Component C5 Extended).

Tier 1 — Fast Signal Pre-Filter (30 seconds, all watchlist tickers):
    Scores every ticker on 3 cheap signals in parallel:
    1. Price momentum:  yfinance batch → movers ±2%+ today
    2. Options flow:    UW flow_alerts → unusual sweeps
    3. Dark pool:       UW dark_pool   → institutional block trades
    Selects top 5 where ≥2 signals CONVERGE in the same direction.

Tier 2 — Deep LLM Analysis (60-90 seconds, top 5 only):
    For each of the top 5 tickers, builds a complete data package:

    STOCK PRICE (most current, pre/post market):
        Webull live position price
        → yfinance fast_info (pre/post market aware)
        → Polygon previous close (fallback)

    OPTIONS DATA (exclusively from UW — matches broker prices):
        Prices:     nbbo_bid / nbbo_ask per contract
        Volume:     volume, sweep_volume, bid_volume, ask_volume
        IV:         implied_volatility per contract (UW, per-contract)
        Greeks:     BSM via py_vollib with UW IV + live spot
        Flow:       flow_alerts (sweeps, size, direction)
        Dark pool:  large institutional prints
        GEX:        gamma exposure walls (where dealers hedge)
        Market tide: call/put ratio (institutional directional bias)
        Earnings:   days to next earnings (IV crush risk)

    LLM receives ALL of the above → decides strategy + strikes
    Python executes arithmetic with real UW prices → complete trade

What makes this unique:
    - Institutional flow × price momentum × dark pool CONVERGENCE
    - YOUR personalized watchlist (not a generic universe)
    - LLM reasons over full picture (not just one signal)
    - Webull-ready order instructions with exact limit price
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date


# ─────────────────────────────────────────────────────────────────────────────
# TIER 1: Fast Signal Pre-Filter
# ─────────────────────────────────────────────────────────────────────────────

def _observed(d: date) -> date:
    if d.weekday() == 5: return d - timedelta(days=1)
    if d.weekday() == 6: return d + timedelta(days=1)
    return d

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    if n > 0:
        first = date(year, month, 1)
        diff  = (weekday - first.weekday()) % 7
        return first + timedelta(days=diff + (n-1)*7)
    last = date(year, month+1, 1) - timedelta(days=1) if month < 12 else date(year+1, 1, 1) - timedelta(days=1)
    diff = (last.weekday() - weekday) % 7
    return last - timedelta(days=diff)

def us_market_holidays(year: int) -> set:
    """NYSE market holidays for any year — computed from rules, not hardcoded."""
    h = set()
    h.add(_observed(date(year, 1, 1)))           # New Year's
    h.add(_nth_weekday(year, 1, 0, 3))            # MLK Day
    h.add(_nth_weekday(year, 2, 0, 3))            # Presidents' Day
    h.add(_nth_weekday(year, 5, 0, -1))           # Memorial Day
    h.add(_observed(date(year, 6, 19)))            # Juneteenth
    h.add(_observed(date(year, 7, 4)))             # Independence Day
    h.add(_nth_weekday(year, 9, 0, 1))             # Labor Day
    h.add(_nth_weekday(year, 11, 3, 4))            # Thanksgiving
    h.add(_observed(date(year, 12, 25)))            # Christmas
    return h

def get_last_trading_date() -> str:
    """
    Return most recent NYSE trading day as 'YYYY-MM-DD'.
    If market is open today, returns today.
    Handles weekends + US federal holidays for any year.
    """
    import pytz
    from datetime import time as dtime
    et    = pytz.timezone("America/New_York")
    now   = datetime.now(et)
    today = now.date()

    # Polygon grouped daily is only available AFTER market close (4PM ET)
    # During market hours, use previous close + Webull live prices for intraday
    if (today.weekday() < 5
            and today not in us_market_holidays(today.year)
            and now.time() >= dtime(16, 0)):
        return today.strftime("%Y-%m-%d")

    # Otherwise walk back to find last trading day
    cursor = today
    for _ in range(14):
        cursor -= timedelta(days=1)
        if cursor.weekday() >= 5:
            continue
        if cursor not in us_market_holidays(cursor.year):
            return cursor.strftime("%Y-%m-%d")
    return (today - timedelta(days=1)).strftime("%Y-%m-%d")


def _is_market_open() -> bool:
    """Check if US market is currently in regular session."""
    try:
        import pytz
        from datetime import time as dtime
        et  = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if now.weekday() >= 5: return False
        t = now.time()
        return dtime(9, 30) <= t < dtime(16, 0)
    except Exception:
        return False


def _get_last_trading_session() -> str:
    try:
        import pytz
        from datetime import time as dtime
        et  = pytz.timezone("America/New_York")
        now = datetime.now(et)
        today = now.date()
        if today.weekday() >= 5 or today in us_market_holidays(today.year):
            return f"Market closed — using {get_last_trading_date()} close"
        t = now.time()
        if t < dtime(9, 30):   return "Pre-market — using yesterday's close"
        if t >= dtime(16, 0):  return "After-hours — using today's close"
        return "Market open"
    except Exception:
        return "Unknown"


def _get_batch_prices(tickers: list[str], user_id: str | None = None) -> dict[str, dict]:
    """
    Get prices for all tickers using correct last trading date.

    Priority:
    1. Polygon grouped daily (ONE call → 12,000+ US stocks, fastest)
    2. yfinance individual (fallback for anything missing)
    3. Broker SDK (for live prices during market hours if available)
    """
    from datetime import date as date_type
    result     = {}
    last_date  = get_last_trading_date()
    session    = _get_last_trading_session()
    ticker_set = set(tickers)
    from datetime import datetime as _dt
    _today = _dt.now().strftime("%Y-%m-%d")
    print(f"[Quick Scan] Market: {session} | Using: {_today}")

    # ── 1. Polygon grouped daily — ONE call, all US stocks ────────────────────
    try:
        from app.utils.config import settings
        import requests as req

        url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{last_date}"
        r   = req.get(url, params={"apiKey": settings.polygon_api_key}, timeout=15)

        if r.status_code == 200:
            for item in r.json().get("results", []):
                sym = item.get("T", "").upper()
                if sym not in ticker_set:
                    continue
                close = float(item.get("c", 0))
                open_ = float(item.get("o", close))
                prev  = float(item.get("pc", open_))
                if close > 0:
                    chg = ((close - prev) / prev * 100) if prev else 0
                    result[sym] = {
                        "price":      round(close, 2),
                        "prev_close": round(prev, 2),
                        "change_pct": round(chg, 2),
                        "volume":     int(item.get("v", 0)),
                        "source":     "polygon_grouped",
                    }
            print(f"[Quick Scan] Polygon: {len(result)}/{len(tickers)} tickers")
        else:
            print(f"[Quick Scan] Polygon {r.status_code} — falling back to yfinance")

    except Exception as e:
        print(f"[Quick Scan] Polygon failed: {e}")

    # ── 2. yfinance for anything Polygon missed ───────────────────────────────
    missing = [t for t in tickers if t not in result]
    if missing:
        try:
            import yfinance as yf
            for ticker in missing:
                try:
                    fi    = yf.Ticker(ticker).fast_info
                    price = fi.get("lastPrice") or fi.get("regularMarketPrice") or 0
                    prev  = fi.get("previousClose") or price
                    if float(price) > 0:
                        chg = ((float(price) - float(prev)) / float(prev) * 100) if prev else 0
                        result[ticker] = {
                            "price":      round(float(price), 2),
                            "prev_close": round(float(prev), 2),
                            "change_pct": round(chg, 2),
                            "volume":     0,
                            "source":     "yfinance",
                        }
                except Exception:
                    pass
            yf_count = sum(1 for t in missing if t in result)
            if yf_count:
                print(f"[Quick Scan] yfinance: {yf_count} additional")
        except Exception:
            pass

    # ── 3. Enhance with live price during market hours (broker SDK) ───────────
    if _is_market_open() and user_id:
        try:
            from app.db.queries.broker_connections import get_broker_credentials
            from app.utils.crypto import decrypt_token
            from webullsdkcore.client import ApiClient
            from webullsdkcore.common.region import Region
            from webullsdktrade.api import API
            creds = get_broker_credentials(user_id, "webull")
            if creds:
                ak, sk = decrypt_token(creds[0]), decrypt_token(creds[1])
                api    = API(ApiClient(ak, sk, Region.US.value))
                for i in range(0, len(tickers), 20):
                    chunk = tickers[i:i+20]
                    try:
                        snap  = api.market_data.get_snapshot(chunk, "stock")
                        items = snap.json() if hasattr(snap, "json") else []
                        if isinstance(items, dict): items = items.get("data", [])
                        for item in (items or []):
                            sym   = item.get("symbol", "").upper()
                            price = float(item.get("last_price") or 0)
                            if sym in result and price > 0:
                                result[sym]["live_price"] = price
                                result[sym]["source"]     = "webull_live"
                    except Exception:
                        pass
            print(f"[Quick Scan] Webull live prices applied during market hours")
        except Exception:
            pass

        # Recalculate change_pct using live price vs prev_close
        updated = 0
        for sym, data in result.items():
            live = data.get("live_price")
            prev = data.get("prev_close") or data.get("price")
            if live and prev and prev > 0:
                data["price"]      = live
                data["change_pct"] = round((live - prev) / prev * 100, 2)
                updated += 1
        if updated:
            print(f"[Quick Scan] Live change_pct updated for {updated} tickers")

    print(f"[Quick Scan] Prices: {len(result)}/{len(tickers)} tickers resolved")
    return result
    """
    Get current/last-close prices for all tickers.

    Priority order (broker-agnostic):
    1. Polygon grouped daily     — one API call, all US stocks, most reliable
    2. Connected broker API      — Webull/Robinhood/IBKR (whoever user connected)
    3. yfinance fast_info        — individual calls, pre/post market aware
    4. Google Finance (fallback) — web scrape as last resort

    On weekends/holidays: returns last available close automatically.
    Momentum = change vs previous close (works on closed days too).
    """
    result  = {}
    session = _get_last_trading_session()
    print(f"[Quick Scan] Market: {session}")

    # ── 1. Polygon grouped daily (one call = all US stocks) ───────────────────
    # Best option: single API call returns close for ALL tickers
    try:
        from app.market_data.polygon_client import get_grouped_daily
        from datetime import timedelta

        # Get last trading day
        today = datetime.now()
        offset = 1
        if today.weekday() == 0:  offset = 3  # Monday → use Friday
        elif today.weekday() == 6: offset = 2  # Sunday → use Friday
        elif today.weekday() == 5: offset = 1  # Saturday → use Friday
        target_date = (today - timedelta(days=offset)).strftime("%Y-%m-%d")

        grouped = get_grouped_daily(target_date)
        ticker_set = set(tickers)
        for item in grouped:
            sym = item.get("T", "").upper()
            if sym not in ticker_set:
                continue
            close = float(item.get("c", 0))
            open_ = float(item.get("o", close))
            prev  = float(item.get("pc", open_))  # prev close if available
            if close > 0:
                chg = ((close - prev) / prev * 100) if prev else 0
                result[sym] = {
                    "price":      round(close, 2),
                    "prev_close": round(prev, 2),
                    "change_pct": round(chg, 2),
                    "source":     "polygon",
                }
        if result:
            print(f"[Quick Scan] Polygon: {len(result)}/{len(tickers)} tickers")
    except Exception as e:
        print(f"[Quick Scan] Polygon grouped failed: {e}")

    # ── 2. Connected broker API (broker-agnostic) ─────────────────────────────
    missing = [t for t in tickers if t not in result]
    if missing and user_id:
        try:
            from app.db.queries.broker_connections import get_all_user_brokers
            from app.utils.current_user import get_current_user_id
            uid = user_id or get_current_user_id()

            # Try each connected broker
            brokers = get_all_user_brokers(uid)
            for broker_name in brokers:
                if not missing: break
                try:
                    if "webull" in broker_name.lower():
                        from app.broker.webull_connector import WebullConnector
                        from webullsdkcore.client import ApiClient
                        from webullsdkcore.common.region import Region
                        from webullsdktrade.api import API
                        from app.db.queries.broker_connections import get_broker_credentials
                        from app.utils.crypto import decrypt_token
                        creds = get_broker_credentials(uid, "webull")
                        ak, sk = decrypt_token(creds[0]), decrypt_token(creds[1])
                        api = API(ApiClient(ak, sk, Region.US.value))
                        for i in range(0, len(missing), 20):
                            chunk = missing[i:i+20]
                            try:
                                snap  = api.market_data.get_snapshot(chunk, "stock")
                                items = snap.json() if hasattr(snap, "json") else []
                                if isinstance(items, dict):
                                    items = items.get("data", [])
                                for item in (items or []):
                                    sym   = item.get("symbol", "").upper()
                                    price = float(item.get("close") or item.get("last_price") or 0)
                                    prev  = float(item.get("prev_close") or price)
                                    if sym and price > 0:
                                        chg = ((price - prev) / prev * 100) if prev else 0
                                        result[sym] = {
                                            "price":      round(price, 2),
                                            "prev_close": round(prev, 2),
                                            "change_pct": round(chg, 2),
                                            "source":     f"webull",
                                        }
                            except Exception:
                                pass
                        broker_count = sum(1 for t in missing if t in result)
                        if broker_count:
                            print(f"[Quick Scan] Webull: {broker_count} additional")
                            missing = [t for t in tickers if t not in result]
                except Exception:
                    pass
        except Exception:
            pass

    # ── 3. yfinance individual (pre/post market aware) ────────────────────────
    missing = [t for t in tickers if t not in result]
    if missing:
        try:
            import yfinance as yf
            for ticker in missing:
                try:
                    fi    = yf.Ticker(ticker).fast_info
                    price = fi.get("lastPrice") or fi.get("regularMarketPrice") or 0
                    prev  = fi.get("previousClose") or price
                    pre   = fi.get("preMarketPrice")
                    post  = fi.get("postMarketPrice")
                    live  = pre or post or price
                    if float(price) > 0:
                        chg = ((float(live) - float(prev)) / float(prev) * 100) if prev else 0
                        result[ticker] = {
                            "price":      round(float(live), 2),
                            "prev_close": round(float(prev), 2),
                            "change_pct": round(chg, 2),
                            "source":     "yfinance",
                            "extended":   bool(pre or post),
                        }
                except Exception:
                    pass
            yf_count = sum(1 for t in missing if t in result)
            if yf_count:
                print(f"[Quick Scan] yfinance: {yf_count} additional")
        except Exception:
            pass

    # ── 4. Google Finance web scrape (last resort) ────────────────────────────
    missing = [t for t in tickers if t not in result]
    if missing:
        try:
            import requests as req
            for ticker in missing[:20]:   # limit scrape calls
                try:
                    url  = f"https://www.google.com/finance/quote/{ticker}:NASDAQ"
                    r    = req.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
                    import re
                    match = re.search(r'"(\d+\.?\d*)"[^}]*"USD"', r.text)
                    if match:
                        price = float(match.group(1))
                        result[ticker] = {
                            "price":      price,
                            "prev_close": price,
                            "change_pct": 0.0,
                            "source":     "google",
                        }
                except Exception:
                    pass
        except Exception:
            pass
    """
    Get current/last-close prices for all tickers.

    Priority:
    1. Webull SDK get_snapshot (batch, official auth, works always)
    2. Polygon get_previous_close (fallback per ticker)
    3. yfinance individual (last resort)

    On weekends/holidays: returns last available close.
    Momentum is always vs previous close (not intraday 0% on weekends).
    """
    result  = {}
    session = _get_last_trading_session()
    print(f"[Quick Scan] Market status: {session}")

    # ── Strategy 1: Webull SDK batch (most reliable) ──────────────────────────
    try:
        from app.db.queries.broker_connections import get_broker_credentials
        from app.utils.current_user import get_current_user_id
        from app.utils.crypto import decrypt_token
        from webullsdkcore.client import ApiClient
        from webullsdkcore.common.region import Region
        from webullsdktrade.api import API

        uid   = user_id or get_current_user_id()
        creds = get_broker_credentials(uid, "webull")
        ak    = decrypt_token(creds[0])
        sk    = decrypt_token(creds[1])
        api   = API(ApiClient(ak, sk, Region.US.value))

        # Batch in chunks of 20 (Webull API limit)
        for i in range(0, len(tickers), 20):
            chunk = tickers[i:i+20]
            try:
                snap = api.market_data.get_snapshot(chunk, "stock")
                data = snap.json() if hasattr(snap, "json") else {}
                items = data if isinstance(data, list) else data.get("data", [])
                for item in items:
                    sym   = item.get("symbol", "").upper()
                    price = float(item.get("close") or item.get("last_price") or 0)
                    prev  = float(item.get("prev_close") or item.get("open") or price)
                    if sym and price > 0:
                        chg = ((price - prev) / prev * 100) if prev else 0
                        result[sym] = {
                            "price":      round(price, 2),
                            "prev_close": round(prev, 2),
                            "change_pct": round(chg, 2),
                            "source":     "webull_sdk",
                        }
            except Exception:
                pass

        if result:
            print(f"[Quick Scan] Webull SDK: {len(result)}/{len(tickers)} prices")
    except Exception as e:
        print(f"[Quick Scan] Webull SDK unavailable: {e}")

    # ── Strategy 2: Polygon fallback for missing tickers ─────────────────────
    missing = [t for t in tickers if t not in result]
    if missing:
        try:
            from app.market_data.uw_market_data import get_previous_close
            for ticker in missing:
                try:
                    q = get_previous_close(ticker)
                    if q and q.get("close"):
                        prev = q.get("open", q["close"])
                        chg  = ((q["close"] - prev) / prev * 100) if prev else 0
                        result[ticker] = {
                            "price":      round(q["close"], 2),
                            "prev_close": round(prev, 2),
                            "change_pct": round(chg, 2),
                            "source":     "polygon",
                        }
                except Exception:
                    pass
            poly_count = sum(1 for t in missing if t in result)
            if poly_count:
                print(f"[Quick Scan] Polygon: {poly_count} additional prices")
        except Exception:
            pass

    # ── Strategy 3: yfinance individual for anything still missing ────────────
    still_missing = [t for t in tickers if t not in result]
    if still_missing:
        try:
            import yfinance as yf
            for ticker in still_missing[:30]:
                try:
                    fi    = yf.Ticker(ticker).fast_info
                    price = fi.get("lastPrice") or fi.get("regularMarketPrice") or 0
                    prev  = fi.get("previousClose") or price
                    if price and float(price) > 0:
                        chg = ((float(price) - float(prev)) / float(prev) * 100) if prev else 0
                        result[ticker] = {
                            "price":      round(float(price), 2),
                            "prev_close": round(float(prev), 2),
                            "change_pct": round(chg, 2),
                            "source":     "yfinance",
                        }
                except Exception:
                    pass
        except Exception:
            pass

    return result


def _get_uw_flow_signals(tickers: list[str]) -> dict[str, dict]:
    """
    Batch fetch ALL flow + dark pool in 2 UW calls instead of 254.
    Was: 127 tickers x 2 calls each = 254 calls, exhausted rate limit.
    Now: 2 batch calls, group by ticker in Python.
    """
    from app.options_flow.unusual_whales import get_flow_alerts, get_dark_pool_recent
    try:
        all_flow = get_flow_alerts(limit=500) or []
        all_dp   = get_dark_pool_recent(limit=200) or []
    except Exception as e:
        print(f"[UW Batch] Failed: {e}")
        return {}
    flow_by = {}
    for a in all_flow:
        flow_by.setdefault(a.get("ticker",""), []).append(a)
    dp_by = {}
    for d in all_dp:
        dp_by.setdefault(d.get("ticker",""), []).append(d)
    print(f"[UW Batch] {len(all_flow)} flow alerts across {len(flow_by)} tickers | "
          f"{len(all_dp)} dp prints across {len(dp_by)} tickers")
    result = {}
    for ticker in tickers:
        alerts    = flow_by.get(ticker, [])
        dp        = dp_by.get(ticker, [])
        if not alerts and not dp:
            continue
        bull_flow  = sum(1 for a in alerts if a.get("sentiment") in ("BULLISH","CALL"))
        bear_flow  = sum(1 for a in alerts if a.get("sentiment") in ("BEARISH","PUT"))
        total_flow = bull_flow + bear_flow
        dp_buy     = sum(1 for d in dp if d.get("side") in ("BUY","A"))
        dp_sell    = sum(1 for d in dp if d.get("side") in ("SELL","B"))
        dp_total   = dp_buy + dp_sell
        flow_score = round((bull_flow-bear_flow)/total_flow*100, 1) if total_flow else 0
        dp_score   = round((dp_buy-dp_sell)/dp_total*100, 1)        if dp_total   else 0
        result[ticker] = {
            "flow_score":  flow_score,
            "dp_score":    dp_score,
            "direction":   "BULLISH" if (flow_score+dp_score) > 0 else "BEARISH",
            "alert_count": total_flow,
            "dp_count":    dp_total,
            "bull_flow":   bull_flow,
            "bear_flow":   bear_flow,
            "sweeps":      sum(1 for a in alerts if a.get("is_sweep")),
        }
    return result

def quick_scan(
    tickers: list[str],
    user_id: str | None = None,
    top_n: int = 5,
    min_convergence: int = 2,   # need ≥2 signals converging
) -> list[dict]:
    """
    TIER 1: Fast pre-filter across all watchlist tickers.

    Scores each ticker on:
    1. Price momentum (±2%+ today)
    2. Options flow (unusual sweeps from UW)
    3. Dark pool (institutional block trades from UW)

    Selects top_n where signals CONVERGE in the same direction.

    Args:
        tickers:         list of tickers to scan (e.g. your 127-stock watchlist)
        top_n:           number of top picks to return (default 5)
        min_convergence: minimum number of signals agreeing (default 2 of 3)

    Returns:
        Ranked list of {ticker, direction, confidence, signals, momentum, flow}
    """
    print(f"[Quick Scan] Scanning {len(tickers)} tickers...")
    t0 = time.time()

    # 1. Batch price check (Polygon grouped daily — one call for all tickers)
    print("[Quick Scan] Fetching prices...")
    prices = _get_batch_prices(tickers, user_id=user_id)

    # 2. UW flow + dark pool (parallel)
    print(f"[Quick Scan] Prices: {len(prices)}/{len(tickers)} tickers")
    print("[Quick Scan] Fetching UW flow signals...")
    flow = _get_uw_flow_signals(tickers)

    # On weekends/holidays, lower thresholds — UW flow is stale, momentum is the key signal
    _session         = _get_last_trading_session()
    market_open      = _is_market_open() or "After-hours" in _session
    mom_threshold    = 1.5 if market_open else 0.5
    min_convergence  = 1  # 1 signal sufficient — convergence improves quality but not required

    # 3. Score each ticker
    scored = []
    for ticker in tickers:
        price_data = prices.get(ticker, {})
        flow_data  = flow.get(ticker, {})

        change_pct = price_data.get("change_pct", 0)
        signals    = []
        directions = []

        # Signal 1: Price momentum (threshold varies: 1.5% live, 0.5% on closed days)
        if abs(change_pct) >= mom_threshold:
            direction = "BULLISH" if change_pct > 0 else "BEARISH"
            signals.append(f"momentum {change_pct:+.1f}%")
            directions.append(direction)

        # Signal 2: Options flow
        if flow_data.get("alert_count", 0) >= 2:
            signals.append(f"flow {flow_data['alert_count']} alerts")
            directions.append(flow_data["direction"])

        # Signal 3: Dark pool
        if flow_data.get("dp_count", 0) >= 1:
            signals.append(f"dark_pool {flow_data['dp_count']} prints")
            directions.append(flow_data["direction"])

        # Signal 4: TA momentum (tiebreaker when flow absent)
        rsi       = float(price_data.get("rsi", 0) or 0)
        sma_50    = float(price_data.get("sma_50", 0) or 0)
        price_val = float(price_data.get("price", 0) or 0)
        chg       = float(price_data.get("change_pct", 0) or 0)
        if sma_50 and price_val > sma_50 * 1.01 and 40 < rsi < 70 and chg > 0.3:
            signals.append(f"ta_bullish rsi={rsi:.0f}")
            directions.append("BULLISH")
        elif sma_50 and price_val < sma_50 * 0.99 and (rsi > 65 or chg < -0.3):
            signals.append(f"ta_bearish rsi={rsi:.0f}")
            directions.append("BEARISH")
        if len(signals) < min_convergence:
            continue

        # Check convergence — all agreeing signals must point same way
        bull = directions.count("BULLISH")
        bear = directions.count("BEARISH")
        if bull >= min_convergence:
            conv_dir   = "BULLISH"
            confidence = round((bull / len(directions)) * 100)
        elif bear >= min_convergence:
            conv_dir   = "BEARISH"
            confidence = round((bear / len(directions)) * 100)
        else:
            continue  # no convergence

        # Overall score: convergence × signal count × flow intensity
        flow_intensity = abs(flow_data.get("flow_score", 0)) if flow_data else 0
        score = (len(signals) / 3) * (confidence / 100) * (1 + flow_intensity / 100)

        scored.append({
            "ticker":      ticker,
            "direction":   conv_dir,
            "confidence":  confidence,
            "score":       round(score, 3),
            "signals":     signals,
            "price":       price_data.get("price", 0),
            "prev_close":  price_data.get("prev_close", 0),
            "change_pct":  change_pct,
            "rsi":         price_data.get("rsi", 0),
            "sma_50":      price_data.get("sma_50", 0),
            "trend":       price_data.get("trend", ""),
            "flow_score":  flow_data.get("flow_score", 0),
            "dp_score":    flow_data.get("dp_score", 0),
            "sweeps":      flow_data.get("sweeps", 0),
            "alert_count": flow_data.get("alert_count", 0),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # On closed days (momentum-only), sort by strongest price move
    if not market_open:
        scored.sort(key=lambda x: abs(x.get("change_pct", 0)), reverse=True)

    elapsed = round(time.time() - t0, 1)
    # Apply velocity multipliers from signal history
    try:
        from app.signals.velocity_tracker import get_velocity_scores, apply_velocity_to_picks
        from app.utils.current_user import get_current_user_id
        uid = user_id or get_current_user_id()
        if uid and scored:
            vel = get_velocity_scores([p["ticker"] for p in scored], uid)
            if vel:
                scored = apply_velocity_to_picks(scored, vel)
                scored.sort(key=lambda x: x["score"], reverse=True)
                print(f"[Quick Scan] Velocity applied: {sum(1 for p in scored if p.get('velocity',0) > 20)} accelerating tickers")
    except Exception as e:
        pass  # velocity scoring optional — never blocks scan

    mode    = "LIVE convergence" if market_open else "HOLIDAY/WEEKEND — momentum only, no live flow"
    print(f"[Quick Scan] Done in {elapsed}s. {len(scored)} picks → top {top_n} | Mode: {mode}")

    return scored[:top_n]


# ─────────────────────────────────────────────────────────────────────────────
# TIER 2: Deep LLM Analysis
# ─────────────────────────────────────────────────────────────────────────────

def _build_full_data_package(
    ticker: str,
    user_id: str | None = None,
) -> dict:
    """
    Build complete data package for one ticker.

    Stock price: Webull → yfinance → Polygon
    Options:     UW exclusively (prices, IV, volume, flow, GEX, market_tide)
    TA:          Technical indicators from our engine
    """
    from datetime import datetime, timedelta
    from app.market_data.uw_market_data import get_bars, get_previous_close
    from app.technical_analysis.engine import get_technical_profile
    from app.options_flow.unusual_whales import (
        get_flow_alerts, get_dark_pool_ticker, get_greek_exposure,
        get_option_contracts, get_expiry_breakdown, get_market_tide,
        get_ticker_earnings_history,
    )

    package = {"ticker": ticker}

    # ── Stock price (Webull → yfinance → Polygon) ─────────────────────────────
    spot = 0
    source = "unknown"

    if user_id:
        try:
            from app.broker.webull_connector import WebullConnector
            wb  = WebullConnector(user_id)
            for p in wb.get_positions():
                if p.get("symbol", "").upper() == ticker and p.get("instrument_type") == "STOCK":
                    spot   = float(p["last_price"])
                    source = "webull_live"
                    break
        except Exception:
            pass

    if not spot:
        try:
            import yfinance as yf
            fi = yf.Ticker(ticker).fast_info
            for k in ("lastPrice", "last_price", "regularMarketPrice"):
                p = fi.get(k)
                if p and float(p) > 0:
                    spot   = float(p)
                    source = "yfinance"
                    break
        except Exception:
            pass

    if not spot:
        try:
            q    = get_previous_close(ticker)
            spot = q.get("close", 0) if q else 0
            source = "polygon"
        except Exception:
            pass

    package["spot"]         = round(spot, 2)
    package["price_source"] = source

    # ── Technical Analysis ────────────────────────────────────────────────────
    try:
        from_date = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
        to_date   = datetime.now().strftime("%Y-%m-%d")
        bars      = get_bars(ticker, 1, "day", from_date, to_date)
        ta        = get_technical_profile(ticker, bars)
        package["ta"] = ta
    except Exception as e:
        package["ta"] = {"error": str(e)}

    # ── UW Options Data (all from UW) ─────────────────────────────────────────
    try:
        # Get all available expiries for context
        import yfinance as yf
        expiries = list(yf.Ticker(ticker).options)[:8]  # first 8 expiries

        # Full options signal package from UW
        package["flow_alerts"]  = get_flow_alerts(ticker=ticker, limit=20)
        package["dark_pool"]    = get_dark_pool_ticker(ticker, limit=10)
        package["gex"]          = get_greek_exposure(ticker)
        package["market_tide"]  = get_market_tide()
        package["earnings"]     = get_ticker_earnings_history(ticker)

        try:
            from app.options_flow.unusual_whales import get_expiry_breakdown
            package["expiry_breakdown"] = get_expiry_breakdown(ticker)
        except Exception:
            package["expiry_breakdown"] = []

        # Options contracts for the most liquid expiry (21-35 DTE)
        today = datetime.now()
        best_expiry = None
        for exp in expiries:
            dte = (datetime.strptime(exp, "%Y-%m-%d") - today).days
            if 14 <= dte <= 45:
                best_expiry = exp
                break

        if best_expiry:
            contracts = get_option_contracts(ticker, expiry=best_expiry, limit=200)
            package["option_contracts"] = contracts
            package["option_expiry"]    = best_expiry

            # Summarize for LLM
            calls = [c for c in contracts if "C" in c.get("option_symbol", "")]
            puts  = [c for c in contracts if "P" in c.get("option_symbol", "")]
            call_vol = sum(int(c.get("volume", 0) or 0) for c in calls)
            put_vol  = sum(int(c.get("volume", 0) or 0) for p in puts)
            sweeps   = [c for c in contracts if int(c.get("sweep_volume", 0) or 0) > 100]

            package["options_summary"] = {
                "expiry":       best_expiry,
                "call_volume":  call_vol,
                "put_volume":   put_vol,
                "put_call_ratio": round(put_vol / max(call_vol, 1), 2),
                "sweep_count":  len(sweeps),
                "top_sweeps":   sorted(sweeps, key=lambda x: int(x.get("sweep_volume",0) or 0), reverse=True)[:5],
                "avg_iv":       round(
                    sum(float(c.get("implied_volatility",0) or 0) for c in contracts[:20]) /
                    max(len(contracts[:20]), 1), 3
                ),
            }

    except Exception as e:
        package["uw_error"] = str(e)

    return package


def deep_analyze(
    quick_picks: list[dict],
    budget: float = 2000.0,
    max_loss: float | None = None,
    min_dte: int = 7,
    max_dte: int = 90,
    user_id: str | None = None,
) -> list[dict]:
    """
    TIER 2: Deep LLM analysis on top picks from quick_scan().

    For each pick:
    1. Build full data package (stock price + TA + all UW options data)
    2. Feed to strategy engine (LLM decides, Python executes math)
    3. Return complete trade recommendation

    Args:
        quick_picks:  output from quick_scan() — top 3-5 tickers
        budget:       capital to deploy per trade
        max_loss:     max acceptable loss per trade
        min_dte:      minimum DTE
        max_dte:      maximum DTE
        user_id:      for Webull live price lookup

    Returns list of complete trade recommendations, ranked by R/R.
    """
    from app.options_flow.signals import score_signal_package
    from app.strategy.engine import build_recommendation

    results = []
    for pick in quick_picks:
        ticker = pick["ticker"]
        print(f"[Deep Scan] Analyzing {ticker} ({pick['direction']}, {pick['confidence']}% confidence)...")

        try:
            # Build complete data package
            pkg = _build_full_data_package(ticker, user_id)

            # Score flow signals
            flow_signal = score_signal_package({
                "ticker":        ticker,
                "flow_alerts":   pkg.get("flow_alerts", []),
                "dark_pool":     pkg.get("dark_pool", []),
                "gex":           pkg.get("gex", {}),
                "market_tide":   pkg.get("market_tide", {}),
                "earnings_history": pkg.get("earnings", []),
                "option_contracts": pkg.get("option_contracts", []),
            })

            # Get strategy recommendation (LLM + Python math)
            rec = build_recommendation(
                ticker        = ticker,
                ta_profile    = pkg["ta"],
                flow_signal   = flow_signal,
                budget        = budget,
                max_loss      = max_loss,
                min_dte       = min_dte,
                max_dte       = max_dte,
                user_id       = user_id,
            )

            # Enrich with quick scan context
            if "best" in rec:
                rec["quick_scan"] = {
                    "signals":     pick["signals"],
                    "change_pct":  pick["change_pct"],
                    "sweeps":      pick["sweeps"],
                    "alert_count": pick["alert_count"],
                }
                rec["options_summary"] = pkg.get("options_summary", {})

            results.append(rec)

        except Exception as e:
            print(f"[Deep Scan] {ticker} failed: {e}")
            results.append({"ticker": ticker, "error": str(e)})

    # Rank by R/R
    def _rr(r):
        try:
            return r.get("best", {}).get("risk_reward") or 0
        except Exception:
            return 0

    results.sort(key=_rr, reverse=True)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def run_full_scan(
    tickers: list[str] | None = None,
    budget: float = 2000.0,
    max_loss: float | None = None,
    top_quick: int = 5,
    user_id: str | None = None,
) -> dict:
    """
    Run the full two-tier scan: quick filter → deep LLM analysis.

    Args:
        tickers:    list of tickers (None = load from Webull watchlist)
        budget:     capital per trade
        max_loss:   max loss per trade
        top_quick:  number of picks from Tier 1 to deep-analyze
        user_id:    for Webull integration

    Returns:
        {
            quick_picks: [{ticker, direction, signals, score, ...}],
            recommendations: [{strategy, legs, target, stop, ...}],
            scan_time_seconds: 95,
        }
    """
    from app.utils.current_user import get_current_user_id

    if user_id is None:
        try:
            user_id = get_current_user_id()
        except Exception:
            pass

    t0 = time.time()

    # Load tickers from Webull watchlist if not provided
    if not tickers:
        from app.scanner.universe import get_scan_universe
        tickers = get_scan_universe(user_id=user_id)

    print(f"\n{'='*50}")
    print(f"CONVERGENCE SCAN — {len(tickers)} tickers")
    print(f"{'='*50}\n")

    # TIER 1: Fast pre-filter
    quick_picks = quick_scan(tickers, user_id=user_id, top_n=top_quick)

    if not quick_picks:
        return {
            "quick_picks":        [],
            "recommendations":    [],
            "message":            "No converging signals found today. Market may be in consolidation.",
            "scan_time_seconds":  round(time.time() - t0, 1),
        }

    print(f"\nTop {len(quick_picks)} converging picks:")
    for p in quick_picks:
        print(f"  {p['ticker']:6} {p['direction']:7} | signals: {', '.join(p['signals'])}")

    # TIER 2: Deep analysis
    print(f"\n[Deep Scan] Running LLM analysis on top {len(quick_picks)} picks...")
    recs = deep_analyze(quick_picks, budget=budget, max_loss=max_loss, user_id=user_id)

    elapsed = round(time.time() - t0, 1)
    print(f"\n✅ Scan complete in {elapsed}s")

    return {
        "quick_picks":        quick_picks,
        "recommendations":    recs,
        "tickers_scanned":    len(tickers),
        "converging_picks":   len(quick_picks),
        "scan_time_seconds":  elapsed,
    }