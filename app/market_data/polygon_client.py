"""
Market Data Service (Component C3).

Primary source: Polygon.io (polygon-api-client SDK).
Fallback: yfinance for basic quotes when Polygon free tier doesn't cover an endpoint.
Local TTL cache: avoids burning API calls on repeated requests within the same session.

Free-tier Polygon endpoints used:
    - get_previous_close_agg()  -> yesterday's OHLCV (daily close price)
    - get_aggs()                -> historical OHLCV bars (1min to monthly)
    - get_ticker_details()      -> name, market cap, sector, industry

Paid-only endpoints (upgrade to Starter $29/mo to unlock):
    - get_snapshot_all()        -> real-time quotes for many tickers
    - get_snapshot_option()     -> options chain with live greeks
    - Real-time WebSocket streams

Upgrade path: change `_get_quote_polygon()` to call `get_snapshot_all()`
once on Starter — the rest of the service doesn't change.
"""
import time
from datetime import datetime, timedelta
from typing import Any

from polygon import RESTClient

from app.utils.config import settings

# ---------------------------------------------------------------------------
# TTL in-memory cache (keyed by (method, *args))
# Resets on process restart — good enough for Phase 1.
# Phase 3+: replace with Redis for persistence across restarts.
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[Any, float]] = {}
_TTL_SECONDS = {
    "quote": 60,          # 1 minute — stale quotes are fine for daily framework
    "bars": 3600,         # 1 hour — historical bars don't change
    "details": 86400,     # 24 hours — market cap/sector rarely changes
    "options_chain": 300, # 5 minutes — options data changes more often
}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None


def _cache_set(key: str, value: Any, ttl: int) -> None:
    _cache[key] = (value, time.time() + ttl)


# ---------------------------------------------------------------------------
# Polygon client (singleton per process)
# ---------------------------------------------------------------------------
_polygon_client: RESTClient | None = None


def _get_client() -> RESTClient:
    global _polygon_client
    if _polygon_client is None:
        if not settings.polygon_api_key:
            raise RuntimeError("POLYGON_API_KEY is not set in .env")
        _polygon_client = RESTClient(settings.polygon_api_key)
    return _polygon_client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_previous_close(ticker: str) -> dict | None:
    """
    Return the previous trading day's OHLCV for a ticker.
    Uses Polygon free-tier endpoint (no real-time data needed).

    Returns: {ticker, open, high, low, close, volume, timestamp}
    """
    key = f"prev_close:{ticker}"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        client = _get_client()
        aggs = list(client.get_previous_close_agg(ticker))
        if not aggs:
            return None
        a = aggs[0]
        result = {
            "ticker": ticker,
            "open": a.open,
            "high": a.high,
            "low": a.low,
            "close": a.close,
            "volume": a.volume,
            "vwap": a.vwap if hasattr(a, "vwap") else None,
            "timestamp": a.timestamp,
            "source": "polygon",
        }
        _cache_set(key, result, _TTL_SECONDS["quote"])
        return result
    except Exception as e:
        print(f"[MarketData] Polygon prev_close failed for {ticker}: {e}")
        return _get_previous_close_yahoo(ticker)


def get_bars(
    ticker: str,
    multiplier: int = 1,
    timespan: str = "day",
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Return OHLCV bars for a ticker.

    Args:
        ticker:      e.g. 'NVDA'
        multiplier:  bar size multiplier (1 = 1 unit of timespan)
        timespan:    'minute' | 'hour' | 'day' | 'week' | 'month'
        from_date:   'YYYY-MM-DD', defaults to 200 trading days ago
        to_date:     'YYYY-MM-DD', defaults to today
        limit:       max bars to return

    Returns: list of {timestamp, open, high, low, close, volume, vwap}
    Sorted oldest → newest (ready for ta indicators).
    """
    if not from_date:
        from_date = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
    if not to_date:
        to_date = datetime.now().strftime("%Y-%m-%d")

    key = f"bars:{ticker}:{multiplier}:{timespan}:{from_date}:{to_date}"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        client = _get_client()
        aggs = list(client.get_aggs(ticker, multiplier, timespan, from_date, to_date, limit=limit))
        result = [
            {
                "timestamp": a.timestamp,
                "open": a.open,
                "high": a.high,
                "low": a.low,
                "close": a.close,
                "volume": a.volume,
                "vwap": a.vwap if hasattr(a, "vwap") else None,
            }
            for a in aggs
        ]
        _cache_set(key, result, _TTL_SECONDS["bars"])
        return result
    except Exception as e:
        print(f"[MarketData] Polygon get_aggs failed for {ticker}: {e}")
        return []


def get_ticker_details(ticker: str) -> dict | None:
    """
    Return fundamental details for a ticker.
    (name, market_cap, sector, industry, exchange, description)
    """
    key = f"details:{ticker}"
    cached = _cache_get(key)
    if cached:
        return cached

    try:
        client = _get_client()
        d = _get_client().get_ticker_details(ticker)
        result = {
            "ticker": ticker,
            "name": d.name,
            "market_cap": d.market_cap,
            "primary_exchange": d.primary_exchange,
            "type": d.type,
            "description": d.description,
            "sic_description": getattr(d, "sic_description", None),
            "total_employees": getattr(d, "total_employees", None),
        }
        _cache_set(key, result, _TTL_SECONDS["details"])
        return result
    except Exception as e:
        print(f"[MarketData] Polygon get_ticker_details failed for {ticker}: {e}")
        return None


def get_bulk_previous_close(tickers: list[str]) -> dict[str, dict]:
    """
    Return previous close for multiple tickers.
    Batches Polygon calls, uses cache where available.

    Returns: {ticker: {open, high, low, close, volume, ...}}
    """
    result: dict[str, dict] = {}
    missed: list[str] = []

    for ticker in tickers:
        cached = _cache_get(f"prev_close:{ticker}")
        if cached:
            result[ticker] = cached
        else:
            missed.append(ticker)

    for ticker in missed:
        data = get_previous_close(ticker)
        if data:
            result[ticker] = data

    return result


# ---------------------------------------------------------------------------
# Yahoo Finance fallback (for basic quotes when Polygon endpoint unavailable)
# ---------------------------------------------------------------------------

def _get_previous_close_yahoo(ticker: str) -> dict | None:
    """Fallback: get previous close from yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="2d")
        if hist.empty:
            return None
        row = hist.iloc[-1]
        return {
            "ticker": ticker,
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": float(row["Volume"]),
            "vwap": None,
            "timestamp": None,
            "source": "yahoo",
        }
    except Exception as e:
        print(f"[MarketData] Yahoo fallback also failed for {ticker}: {e}")
        return None