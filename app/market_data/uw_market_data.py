"""
UW Market Data - Drop-in replacement for polygon_client get_bars/get_previous_close.
Uses UW paid API (no rate limits, 0.15s per call) as primary.
Polygon grouped_daily kept for scanner (all-ticker batch call).
"""
from datetime import datetime


def get_bars(ticker, multiplier=1, timespan="day", from_date=None, to_date=None, limit=300):
    """UW OHLC first (0.15s), Polygon fallback."""
    try:
        from app.options_flow.unusual_whales import get_ohlc
        timespan_map = {"minute": "1m", "hour": "1h", "day": "1d", "week": "1d"}
        bars = get_ohlc(ticker, candle_size=timespan_map.get(timespan, "1d"), limit=limit)
        if bars:
            if from_date:
                from_ts = int(datetime.strptime(from_date, "%Y-%m-%d").timestamp() * 1000)
                bars = [b for b in bars if b["t"] >= from_ts]
            if to_date:
                to_ts = int(datetime.strptime(to_date, "%Y-%m-%d").timestamp() * 1000)
                bars = [b for b in bars if b["t"] <= to_ts]
            if bars:
                return bars
    except Exception as e:
        print(f"[UW] get_bars {ticker}: {e}")
    from app.market_data.polygon_client import get_bars as _pg
    return _pg(ticker, multiplier, timespan, from_date, to_date)


def get_previous_close(ticker):
    """UW live price first, Polygon fallback."""
    try:
        from app.options_flow.unusual_whales import get_stock_state
        s = get_stock_state(ticker)
        if s and s.get("price"):
            return float(s["price"])
    except Exception as e:
        print(f"[UW] get_previous_close {ticker}: {e}")
    from app.market_data.polygon_client import get_previous_close as _pg
    return _pg(ticker)


def get_real_iv_rank(ticker):
    """Real 1-year IV rank from UW."""
    try:
        from app.options_flow.unusual_whales import get_iv_rank
        return get_iv_rank(ticker)
    except Exception:
        return None
