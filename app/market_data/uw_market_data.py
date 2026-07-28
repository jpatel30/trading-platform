"""
UW Market Data - Drop-in replacement for polygon_client get_bars/get_previous_close.
Uses UW paid API (no rate limits, 0.15s per call) as primary.
Polygon grouped_daily kept for scanner (all-ticker batch call).
"""
from datetime import datetime


def get_bars(ticker, multiplier=1, timespan="day", from_date=None, to_date=None, limit=300):
    """UW OHLC first (0.15s), Polygon fallback.

    multiplier is only meaningful for minute/hour granularities — UW's
    candle_size is a single string like "5m"/"15m"/"1h" (confirmed live:
    it accepts 1m/5m/15m/30m/1h/4h, but rejects "2d" with a 422, and has
    no monthly candle under any tested format). Below day/week this now
    actually builds the requested candle size instead of silently
    ignoring multiplier and always returning single-unit bars — the
    previous version's timespan_map mapped EVERY "minute" request to a
    flat "1m", regardless of what multiplier said, which is exactly the
    bug intraday_entry.py found and worked around by calling get_ohlc()
    directly instead of going through this wrapper.
    """
    try:
        from app.options_flow.unusual_whales import get_ohlc
        if timespan == "minute":
            candle_size = f"{int(multiplier)}m"
        elif timespan == "hour":
            candle_size = f"{int(multiplier)}h"
        elif timespan == "week":
            candle_size = "1w"   # was wrongly "1d" — week never actually meant daily
        else:
            # day (and month — UW has no real candle for that at all,
            # see REMAINING_ITEMS.md). Multiplier has no real meaning
            # here (UW rejects e.g. "2d"), so it's intentionally not
            # applied — but flag it if a caller ever asks, since that
            # combination is silently unsupported by the real API.
            if multiplier != 1:
                print(f"[UW] get_bars {ticker}: multiplier={multiplier} has no effect "
                      f"for timespan='{timespan}' — UW only supports single-unit day/week candles")
            candle_size = "1d"
        bars = get_ohlc(ticker, candle_size=candle_size, limit=limit)
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
