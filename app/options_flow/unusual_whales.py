"""
Unusual Whales API Client (Component C4 — Options Flow Service).

Covers all trading-relevant endpoints:
    Options Flow:   flow_alerts, option_contracts, expiry_breakdown, oi_change
    Dark Pool:      recent trades, ticker-specific trades
    GEX/Greeks:     greek_exposure, gex_by_strike, market_tide
    Market:         total_options_volume, sector_etfs, net_flow_expiry
    Calendar:       earnings_afterhours, earnings_premarket, economic_calendar
    Fundamentals:   ticker_earnings_history, company_profile, insider_flow
    Alternative:    congress_trades, news_headlines, lit_flow

Endpoints returning 403 on current plan: movers, ipo_calendar
All responses follow {"data": [...]} pattern.

Authentication: Bearer token in Authorization header.
"""
import time
from typing import Any

import requests

from app.utils.config import settings

# ─────────────────────────────────────────────────────────────────────────────
# TTL Cache — resets on process restart
# ─────────────────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[Any, float]] = {}

_TTL = {
    "flow_alerts":     60,      # 1 min — options flow is real-time
    "dark_pool":       60,      # 1 min
    "gex":             120,     # 2 min — GEX changes slowly intraday
    "market_tide":     60,      # 1 min
    "oi_change":       300,     # 5 min
    "sector_etfs":     300,     # 5 min
    "earnings":        3600,    # 1 hour
    "econ_calendar":   3600,    # 1 hour
    "news":            120,     # 2 min
    "congress":        3600,    # 1 hour
    "insider":         1800,    # 30 min
    "contracts":       120,     # 2 min
    "expiry":          120,     # 2 min
    "lit_flow":        60,      # 1 min
}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    return entry[0] if entry and time.time() < entry[1] else None


def _cache_set(key: str, value: Any, ttl_key: str) -> None:
    _cache[key] = (value, time.time() + _TTL[ttl_key])


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Client
# ─────────────────────────────────────────────────────────────────────────────
BASE = "https://api.unusualwhales.com"


def _get(path: str, params: dict | None = None) -> Any:
    """Make a GET request, return response .json()['data'] or full response."""
    if not settings.unusual_whales_token:
        raise RuntimeError("UNUSUAL_WHALES_TOKEN is not set in .env")

    headers = {
        "Authorization": f"Bearer {settings.unusual_whales_token}",
        "Accept": "application/json",
    }
    r = requests.get(f"{BASE}{path}", headers=headers, params=params, timeout=10)
    if r.status_code == 429:
        print(f"[UW] Rate limited (429) on {path} — returning empty result")
        return {}
    r.raise_for_status()
    body = r.json()
    return body.get("data", body)


# ─────────────────────────────────────────────────────────────────────────────
# OPTIONS FLOW
# ─────────────────────────────────────────────────────────────────────────────

def get_flow_alerts(
    ticker: str | None = None,
    min_premium: float = 0,
    sweeps_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """
    Recent options flow alerts.
    Fields: ticker, type (call/put), total_premium, has_sweep, volume,
            open_interest, volume_oi_ratio, underlying_price, strike,
            expiry, total_ask_side_prem, total_bid_side_prem, sector,
            next_earnings_date, start_time, end_time, trade_count

    Args:
        ticker:       filter to one ticker (None = all market)
        min_premium:  minimum total premium in dollars (e.g. 500000 = $500K)
        sweeps_only:  if True, only return sweep alerts
        limit:        max results
    """
    key = f"flow:{ticker}:{min_premium}:{sweeps_only}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get("/api/option-trades/flow-alerts")
    if not isinstance(data, list):
        return []

    # Filter
    if ticker:
        data = [d for d in data if d.get("ticker", "").upper() == ticker.upper()]
    if sweeps_only:
        data = [d for d in data if d.get("has_sweep")]
    if min_premium > 0:
        data = [d for d in data if float(d.get("total_premium") or 0) >= min_premium]

    result = data[:limit]
    _cache_set(key, result, "flow_alerts")
    return result


def get_option_contracts(ticker: str, expiry: str | None = None, limit: int = 100) -> list[dict]:
    """
    Active option contracts for a ticker, optionally filtered by expiry date.

    Args:
        ticker: e.g. 'NVDA'
        expiry: 'YYYY-MM-DD' to filter to specific expiry (REQUIRED for accurate pricing)
                Without expiry, UW returns today's 500 most active contracts
                which on high-volume days (FOMC, earnings) are all 0DTE.
        limit:  max contracts to return

    Fields: option_symbol, nbbo_bid, nbbo_ask, last_price, implied_volatility,
            volume, open_interest, sweep_volume, avg_price, total_premium

    Pricing note:
        Use nbbo_bid and nbbo_ask directly — these match broker prices.
        Mid = (nbbo_bid + nbbo_ask) / 2 for fair value estimate.
        Use implied_volatility with py_vollib for greeks calculation.
    """
    key = f"contracts:{ticker}:{expiry}"
    cached = _cache_get(key)
    if cached:
        return cached

    params = {}
    if expiry:
        params["expiry"] = expiry

    data = _get(f"/api/stock/{ticker.upper()}/option-contracts", params=params)
    result = data[:limit] if isinstance(data, list) else []
    _cache_set(key, result, "contracts")
    return result


def get_expiry_breakdown(ticker: str) -> list[dict]:
    """
    Options open interest and volume by expiry date.
    Fields: expires, volume, open_interest, chains
    Useful for finding where most action is concentrated.
    """
    key = f"expiry:{ticker}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get(f"/api/stock/{ticker.upper()}/expiry-breakdown")
    result = data if isinstance(data, list) else []
    _cache_set(key, result, "expiry")
    return result


def get_oi_change(limit: int = 50) -> list[dict]:
    """
    Contracts with biggest open interest changes today.
    Fields: underlying_symbol, option_symbol, oi_change, curr_oi, volume,
            avg_price, days_of_oi_increases, days_of_vol_greater_than_oi,
            prev_ask_volume, prev_bid_volume, prev_total_premium

    days_of_oi_increases > 3 = sustained institutional accumulation.
    """
    cached = _cache_get("oi_change_global")
    if cached:
        return cached

    data = _get("/api/market/oi-change")
    result = data[:limit] if isinstance(data, list) else []
    _cache_set("oi_change_global", result, "oi_change")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DARK POOL
# ─────────────────────────────────────────────────────────────────────────────

def get_dark_pool_recent(min_premium: float = 0, limit: int = 50) -> list[dict]:
    """
    Recent dark pool (off-exchange) trades across all tickers.
    Fields: ticker, premium, size, price, volume, executed_at,
            nbbo_ask, nbbo_bid, market_center, trade_code

    Large premium ($2M+) = institutional block trade. Compare price to
    nbbo_bid/ask to determine if trade was above or below market.
    """
    key = f"dp_recent:{min_premium}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get("/api/darkpool/recent")
    if isinstance(data, list):
        if min_premium > 0:
            data = [d for d in data if float(d.get("premium") or 0) >= min_premium]
        data = data[:limit]

    _cache_set(key, data, "dark_pool")
    return data if isinstance(data, list) else []


def get_dark_pool_ticker(ticker: str, limit: int = 50) -> list[dict]:
    """
    Dark pool trades for a specific ticker.
    Same fields as get_dark_pool_recent() but filtered to one ticker.
    """
    key = f"dp:{ticker}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get(f"/api/darkpool/{ticker.upper()}")
    result = data[:limit] if isinstance(data, list) else []
    _cache_set(key, result, "dark_pool")
    return result


def get_lit_flow_recent(limit: int = 50) -> list[dict]:
    """
    Recent lit (on-exchange) large block trades.
    Fields: ticker, premium, size, price, volume, executed_at
    Complement to dark pool — together give full institutional picture.
    """
    cached = _cache_get("lit_flow_recent")
    if cached:
        return cached

    data = _get("/api/lit-flow/recent")
    result = data[:limit] if isinstance(data, list) else []
    _cache_set("lit_flow_recent", result, "lit_flow")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GEX / GREEK EXPOSURE
# ─────────────────────────────────────────────────────────────────────────────

def get_greek_exposure(ticker: str) -> list[dict]:
    """
    Dealer Greek exposure over time for a ticker.
    Fields: date, call_delta, put_delta, call_gamma, put_gamma,
            call_vanna, put_vanna, call_charm, put_charm

    Negative net gamma = dealers short gamma = volatile, moves amplified.
    Positive net gamma = dealers long gamma = stabilizing, mean-reverting.
    """
    key = f"gex:{ticker}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get(f"/api/stock/{ticker.upper()}/greek-exposure")
    result = data if isinstance(data, list) else []
    _cache_set(key, result, "gex")
    return result


def get_gex_by_strike(ticker: str) -> list[dict]:
    """
    Dealer GEX by strike price for a ticker.
    Fields: strike, call_gex, put_gex, call_delta, put_delta,
            call_vanna, put_vanna, call_charm, put_charm

    The strike with highest net GEX is the "gamma wall" — strong
    gravitational level. Negative GEX strikes = potential acceleration.
    """
    key = f"gex_strike:{ticker}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get(f"/api/stock/{ticker.upper()}/greek-exposure/strike")
    result = data if isinstance(data, list) else []
    _cache_set(key, result, "gex")
    return result


def get_gex_by_expiry(ticker: str) -> list[dict]:
    """GEX breakdown by expiry date for a ticker."""
    key = f"gex_expiry:{ticker}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get(f"/api/stock/{ticker.upper()}/greek-exposure/expiry")
    result = data if isinstance(data, list) else []
    _cache_set(key, result, "gex")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# MARKET-WIDE SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

def get_market_tide() -> list[dict]:
    """
    Intraday market net flow (calls vs puts premium).
    Fields: timestamp, date, net_call_premium, net_put_premium, net_volume

    net_call_premium > net_put_premium = bullish institutional flow.
    Tracks the "smart money tide" throughout the day.
    """
    cached = _cache_get("market_tide")
    if cached:
        return cached

    data = _get("/api/market/market-tide")
    result = data if isinstance(data, list) else []
    _cache_set("market_tide", result, "market_tide")
    return result


def get_total_options_volume() -> dict:
    """
    Today's total market options volume and premium.
    Fields: date, call_volume, put_volume, call_premium, put_premium

    call_premium / put_premium ratio = market-wide sentiment.
    """
    cached = _cache_get("total_oi_vol")
    if cached:
        return cached

    data = _get("/api/market/total-options-volume")
    result = data[0] if isinstance(data, list) and data else {}
    _cache_set("total_oi_vol", result, "oi_change")
    return result


def get_net_flow_by_expiry() -> list[dict]:
    """
    Net options flow broken down by expiry date.
    Fields: data (nested), moneyness, tide_type
    Shows where institutional money is concentrated by expiry.
    """
    cached = _cache_get("net_flow_expiry")
    if cached:
        return cached

    data = _get("/api/net-flow/expiry")
    result = data if isinstance(data, list) else []
    _cache_set("net_flow_expiry", result, "market_tide")
    return result


def get_sector_etfs() -> list[dict]:
    """
    Options flow and price data for all sector ETFs.
    Fields: ticker, full_name, last, call_volume, put_volume,
            call_premium, put_premium, bearish_premium, bullish_premium,
            avg30_call_volume, avg30_put_volume, in_out_flow

    Use to identify which sectors are seeing bullish/bearish flow.
    """
    cached = _cache_get("sector_etfs")
    if cached:
        return cached

    data = _get("/api/market/sector-etfs")
    result = data if isinstance(data, list) else []
    _cache_set("sector_etfs", result, "sector_etfs")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CALENDAR & EVENTS
# ─────────────────────────────────────────────────────────────────────────────

def get_earnings_afterhours() -> list[dict]:
    """
    Earnings reports scheduled for after market close today.
    Fields: symbol, full_name, sector, expected_move, expected_move_perc,
            street_mean_est, actual_eps, reaction, report_date, has_options
    """
    cached = _cache_get("earnings_ah")
    if cached:
        return cached

    data = _get("/api/earnings/afterhours")
    result = data if isinstance(data, list) else []
    _cache_set("earnings_ah", result, "earnings")
    return result


def get_earnings_premarket() -> list[dict]:
    """
    Earnings reports scheduled for before market open today.
    Same fields as get_earnings_afterhours().
    """
    cached = _cache_get("earnings_pm")
    if cached:
        return cached

    data = _get("/api/earnings/premarket")
    result = data if isinstance(data, list) else []
    _cache_set("earnings_pm", result, "earnings")
    return result


def get_ticker_earnings_history(ticker: str) -> list[dict]:
    """
    Historical earnings data for a ticker (up to 104 quarters).
    Fields: report_date, expected_move_perc, actual_eps, street_mean_est,
            post_earnings_move_1d, post_earnings_move_1w, pre_earnings_move_1w,
            long_straddle_1d, short_straddle_1d

    Crucial for: expected move sizing, historical reaction patterns,
    whether a long/short straddle has historically been profitable.
    """
    key = f"earnings:{ticker}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get(f"/api/earnings/{ticker.upper()}")
    result = data if isinstance(data, list) else []
    _cache_set(key, result, "earnings")
    return result


def get_economic_calendar() -> list[dict]:
    """
    Upcoming economic events (CPI, FOMC, GDP, jobs, etc.).
    Fields: type, event, time, prev, forecast, reported_period

    CRITICAL for Rule 3 regime check — identifies binary events
    within the next 5 days that require reducing position size.
    """
    cached = _cache_get("econ_calendar")
    if cached:
        return cached

    data = _get("/api/market/economic-calendar")
    result = data if isinstance(data, list) else []
    _cache_set("econ_calendar", result, "econ_calendar")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# NEWS & ALTERNATIVE DATA
# ─────────────────────────────────────────────────────────────────────────────

def get_news_headlines(ticker: str | None = None, limit: int = 20) -> list[dict]:
    """
    Recent market news headlines with sentiment scoring.
    Fields: headline, source, created_at, tags, tickers, is_major, sentiment

    sentiment: bullish/bearish/neutral per headline.
    Filter by ticker to get ticker-specific news catalyst.
    """
    key = f"news:{ticker or 'all'}"
    cached = _cache_get(key)
    if cached:
        return cached

    # Server-side ticker filtering — more reliable than client-side
    params = {"limit": min(limit, 100)}
    if ticker:
        params["ticker"] = ticker.upper()
    data = _get("/api/news/headlines", params)
    data = data[:limit] if isinstance(data, list) else []

    _cache_set(key, data, "news")
    return data


def get_congress_trades(ticker: str | None = None, limit: int = 20) -> list[dict]:
    """
    Recent congressional stock trades (senators + representatives).
    Fields: name, ticker, transaction_date, txn_type (buy/sell),
            amounts, member_type, notes

    Congressional buy signals have historically preceded major moves.
    """
    key = f"congress:{ticker or 'all'}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get("/api/congress/recent-trades")
    if isinstance(data, list):
        if ticker:
            data = [d for d in data if (d.get("ticker") or "").upper() == ticker.upper()]
        data = data[:limit]

    _cache_set(key, data, "congress")
    return data if isinstance(data, list) else []


def get_insider_transactions(ticker: str | None = None, limit: int = 20) -> list[dict]:
    """
    Recent insider (Form 4) transactions.
    Fields: ticker, owner_name, transaction_code (P=purchase, S=sale),
            amount, price, shares_owned_after, filing_date, is_director,
            is_officer, is_10b5_1

    Cluster of insider BUYS (not preplanned 10b5-1) = strong signal.
    """
    cached = _cache_get("insider_all")
    if not cached:
        data = _get("/api/insider/transactions")
        _cache_set("insider_all", data, "insider")
        cached = data

    if not isinstance(cached, list):
        return []

    if ticker:
        cached = [d for d in cached if (d.get("ticker") or "").upper() == ticker.upper()]

    return cached[:limit]


def get_insider_ticker_flow(ticker: str) -> list[dict]:
    """
    Daily insider buy/sell flow for a specific ticker.
    Fields: date, volume, premium, avg_price, buy_sell, uniq_insiders,
            premium_10b5, transactions_10b5
    """
    key = f"insider_flow:{ticker}"
    cached = _cache_get(key)
    if cached:
        return cached

    data = _get(f"/api/insider/{ticker.upper()}/ticker-flow")
    result = data if isinstance(data, list) else []
    _cache_set(key, result, "insider")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# COMPREHENSIVE SIGNAL PACKAGE
# ─────────────────────────────────────────────────────────────────────────────

def get_signal_package(ticker: str, target_expiry: str | None = None) -> dict:
    """
    Fetch ALL relevant signals for one ticker in a single call.

    Args:
        ticker:         stock ticker e.g. 'NVDA'
        target_expiry:  'YYYY-MM-DD' — the expiry date for the trade being considered.
                        If None, auto-selects the nearest Friday 3 weeks out.
                        IMPORTANT: always pass the actual target expiry so option
                        contracts are fetched for that cycle, not today's 0DTE.
    """
    ticker = ticker.upper()

    # Auto-select target expiry if not provided (3 weeks out)
    if not target_expiry:
        from datetime import datetime, timedelta
        today = datetime.now()
        days_until_friday = (4 - today.weekday()) % 7 or 7
        target_expiry = (today + timedelta(days=days_until_friday + 14)).strftime("%Y-%m-%d")

    return {
        "ticker": ticker,
        "target_expiry": target_expiry,
        "flow_alerts": get_flow_alerts(ticker=ticker, limit=20),
        "dark_pool": get_dark_pool_ticker(ticker, limit=20),
        "gex": get_greek_exposure(ticker),
        "gex_by_strike": get_gex_by_strike(ticker),
        "option_contracts": get_option_contracts(ticker, expiry=target_expiry, limit=500),
        "expiry_breakdown": get_expiry_breakdown(ticker),
        "earnings_history": get_ticker_earnings_history(ticker),
        "insider_flow": get_insider_ticker_flow(ticker),
        "news": get_news_headlines(ticker=ticker, limit=10),
        "congress_trades": get_congress_trades(ticker=ticker, limit=5),
        "market_tide": get_market_tide(),
    }

# ─────────────────────────────────────────────────────────────────────────────
# UW Market Data
# ─────────────────────────────────────────────────────────────────────────────

def get_ohlc(ticker: str, candle_size: str = "1d", limit: int = 300) -> list[dict]:
    """
    OHLC bars from UW — returns Polygon-compatible format.
    UW returns a list directly (not {"data": [...]}).
    market_time values: "r"=regular, "pr"=premarket, "po"=postmarket
    """
    bars = _get(f"/api/stock/{ticker.upper()}/ohlc/{candle_size}", {"limit": limit})
    if not bars or not isinstance(bars, list):
        return []
    import datetime as _dt
    result = []
    for b in bars:
        try:
            # Only regular session bars
            if b.get("market_time") != "r":
                continue
            date_str = b.get("date", "")
            ts = int(_dt.datetime.strptime(
                date_str[:10], "%Y-%m-%d").timestamp() * 1000) if date_str else 0
            result.append({
                "c":  float(b.get("close", 0) or 0),
                "h":  float(b.get("high",  0) or 0),
                "l":  float(b.get("low",   0) or 0),
                "o":  float(b.get("open",  0) or 0),
                "v":  int(b.get("total_volume", 0) or b.get("volume", 0) or 0),
                "t":  ts,
                "vw": float(b.get("close", 0) or 0),
            })
        except Exception:
            continue
    return sorted(result, key=lambda x: x["t"])


def get_stock_state(ticker: str) -> dict | None:
    """
    Live stock price from UW including pre/post market.
    UW returns a list with one item for stock-state.
    """
    data = _get(f"/api/stock/{ticker.upper()}/stock-state")
    # Handle both list and dict responses
    if isinstance(data, list):
        d = data[0] if data else {}
    elif isinstance(data, dict):
        d = data.get("data", data)
    else:
        return None
    if not d:
        return None
    return {
        "price":       float(d.get("close",      0) or 0),
        "close":       float(d.get("close",      0) or 0),
        "prev_close":  float(d.get("prev_close", 0) or 0),
        "high":        float(d.get("high",       0) or 0),
        "low":         float(d.get("low",        0) or 0),
        "open":        float(d.get("open",       0) or 0),
        "volume":      int(d.get("total_volume", 0) or d.get("volume", 0) or 0),
        "market_time": d.get("market_time", "regular"),
        "tape_time":   d.get("tape_time", ""),
    }


def get_iv_rank(ticker: str) -> dict | None:
    """
    Real 1-year IV rank from UW.
    UW returns a list of daily IV rank records.
    """
    rows = _get(f"/api/stock/{ticker.upper()}/iv-rank")
    if not rows or not isinstance(rows, list):
        return None
    latest = rows[-1] if rows else {}
    return {
        "iv_rank":    float(latest.get("iv_rank_1y", 50) or 50),
        "iv_current": float(latest.get("volatility",  0) or 0),
        "close":      float(latest.get("close",       0) or 0),
        "date":       latest.get("date", ""),
        "source":     "uw_1y",
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW UW INTEGRATIONS (paid plan — 200 confirmed)
# ─────────────────────────────────────────────────────────────────────────────

def get_institutional_ownership(ticker: str) -> dict:
    """
    Institutional ownership for ticker.
    Returns top institutions, total value, and ownership concentration.
    Used in conviction scoring: high institutional ownership = stable thesis.
    """
    data = _get(f"/api/institution/{ticker.upper()}/ownership")
    if not data or not isinstance(data, list):
        return {"error": "No data", "score": 50}

    total_value    = sum(float(d.get("value", 0) or 0) for d in data)
    institution_ct = len(data)
    top_holders    = [d.get("name", "") for d in data[:5]]

    # Score 0-100: more institutions + higher value = higher score
    score = min(100, int(institution_ct / 2) + (30 if total_value > 1e11 else 15 if total_value > 1e10 else 5))

    return {
        "institution_count": institution_ct,
        "total_value":       total_value,
        "top_holders":       top_holders,
        "score":             score,
        "note":              f"{institution_ct} institutions, top: {', '.join(top_holders[:2])}",
    }


def get_greek_flow(ticker: str) -> dict:
    """
    Greek flow — call vs put gamma/delta direction signal.
    Returns net call vs put direction from greek flow data.
    """
    data = _get(f"/api/stock/{ticker.upper()}/greek-flow")
    if not data or not isinstance(data, dict):
        return {"direction": "NEUTRAL", "score": 50}
    rows = data.get("data", [])
    if not rows:
        return {"direction": "NEUTRAL", "score": 50}

    # Sum recent call vs put transactions
    call_txn = sum(int(r.get("call_transactions", 0) or 0) for r in rows[-5:])
    put_txn  = sum(int(r.get("put_transactions",  0) or 0) for r in rows[-5:])
    total    = call_txn + put_txn

    if total == 0:
        return {"direction": "NEUTRAL", "score": 50}

    call_ratio = call_txn / total
    if call_ratio >= 0.65:
        direction, score = "BULLISH", round(call_ratio * 100)
    elif call_ratio <= 0.35:
        direction, score = "BEARISH", round((1 - call_ratio) * 100)
    else:
        direction, score = "NEUTRAL", 50

    return {
        "direction":    direction,
        "score":        score,
        "call_txn":     call_txn,
        "put_txn":      put_txn,
        "call_ratio":   round(call_ratio, 2),
        "note":         f"Greek flow: {direction} ({call_ratio:.0%} calls)",
    }


def get_net_premium_ticks(ticker: str) -> dict:
    """
    Net premium ticks — call vs put net premium today.
    Most reliable intraday direction signal.
    call_premium > put_premium = bullish institutional positioning.
    """
    data = _get(f"/api/stock/{ticker.upper()}/net-prem-ticks")
    if not data or not isinstance(data, dict):
        return {"direction": "NEUTRAL", "score": 50}
    rows = data.get("data", [])
    if not rows:
        return {"direction": "NEUTRAL", "score": 50}

    latest = rows[-1] if rows else {}
    call_vol   = float(latest.get("call_volume",     0) or 0)
    put_vol    = float(latest.get("put_volume",      0) or 0)
    call_prem  = float(latest.get("call_premium",    0) or 0)
    put_prem   = float(latest.get("put_premium",     0) or 0)

    total_prem = call_prem + put_prem
    if total_prem == 0:
        return {"direction": "NEUTRAL", "score": 50}

    call_ratio = call_prem / total_prem
    if call_ratio >= 0.60:
        direction, score = "BULLISH", round(call_ratio * 100)
    elif call_ratio <= 0.40:
        direction, score = "BEARISH", round((1 - call_ratio) * 100)
    else:
        direction, score = "NEUTRAL", 50

    return {
        "direction":   direction,
        "score":       score,
        "call_premium": call_prem,
        "put_premium":  put_prem,
        "call_ratio":   round(call_ratio, 2),
        "note":         f"Net premium: {direction} (calls {call_ratio:.0%} of total)",
    }


def get_etf_sector_flow(etf: str) -> dict:
    """
    ETF in/outflow — money moving in or out of sector.
    Positive change = money flowing in (sector bullish).
    Used for sector-level conviction in recommendations.
    """
    data = _get(f"/api/etfs/{etf.upper()}/in-outflow")
    if not data or not isinstance(data, dict):
        return {"direction": "NEUTRAL", "net_flow": 0}
    rows = data.get("data", [])
    if not rows:
        return {"direction": "NEUTRAL", "net_flow": 0}

    # Sum last 5 days
    recent   = rows[-5:]
    net_flow = sum(float(r.get("change", 0) or 0) for r in recent)

    if net_flow > 5_000_000:
        direction = "BULLISH"
    elif net_flow < -5_000_000:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "net_flow":  net_flow,
        "etf":       etf.upper(),
        "note":      f"{etf} flow: {direction} (${net_flow:,.0f} net 5d)",
    }
