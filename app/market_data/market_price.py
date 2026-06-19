"""
Market Price Service — gets the most current price for any ticker.

Priority order:
1. Webull positions (if user owns the stock — most current, includes pre/post)
2. yfinance (includes pre/post market, real-time delayed ~15min)
3. Unusual Whales (last_price from option contracts — intraday)
4. Polygon.io (previous close — fallback)

Returns:
    {
        ticker:  "NVDA",
        price:   205.64,
        session: "REGULAR" | "PRE_MARKET" | "POST_MARKET" | "PREVIOUS_CLOSE",
        source:  "webull" | "yfinance" | "polygon",
        change:  +1.24,
        change_pct: +0.61,
        timestamp: "2026-06-17T15:45:00"
    }
"""
from datetime import datetime, time
import pytz


def _is_market_hours() -> str:
    """Return current market session."""
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    t   = now.time()

    if time(4, 0) <= t < time(9, 30):   return "PRE_MARKET"
    if time(9, 30) <= t < time(16, 0):  return "REGULAR"
    if time(16, 0) <= t < time(20, 0):  return "POST_MARKET"
    return "CLOSED"


def get_market_price(ticker: str, user_id: str | None = None) -> dict:
    """
    Get the most current price including pre/post market.

    Args:
        ticker:  stock ticker e.g. 'NVDA'
        user_id: optional — if provided, checks Webull positions first
    """
    ticker  = ticker.upper()
    session = _is_market_hours()

    # ── Source 1: Webull positions (if user owns this ticker) ────────────────
    if user_id:
        try:
            from app.broker.webull_connector import WebullConnector
            from app.broker.base import BrokerNotConnectedError
            wb = WebullConnector(user_id)
            positions = wb.get_positions()
            for p in positions:
                if p.get("symbol", "").upper() == ticker and p.get("instrument_type") == "STOCK":
                    price = float(p["last_price"])
                    cost  = float(p["unit_cost"])
                    return {
                        "ticker": ticker,
                        "price":  price,
                        "session": session,
                        "source": "webull_position",
                        "change": round(price - cost, 2),
                        "change_pct": round((price - cost) / cost * 100, 2) if cost else None,
                        "timestamp": datetime.now().isoformat(),
                        "note": "From your Webull position (live price)"
                    }
        except Exception:
            pass

    # ── Source 2: yfinance — includes pre/post market ─────────────────────────
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        fi = t.fast_info

        # Regular session price
        regular_price = fi.get("regularMarketPrice") or fi.get("last_price")

        # Extended hours
        pre_price  = fi.get("preMarketPrice")
        post_price = fi.get("postMarketPrice")

        prev_close = fi.get("previousClose") or fi.get("regular_market_previous_close")

        if session == "PRE_MARKET" and pre_price and float(pre_price) > 0:
            price = float(pre_price)
            label = "PRE_MARKET"
        elif session == "POST_MARKET" and post_price and float(post_price) > 0:
            price = float(post_price)
            label = "POST_MARKET"
        elif regular_price and float(regular_price) > 0:
            price = float(regular_price)
            label = session if session == "REGULAR" else "PREVIOUS_CLOSE"
        else:
            raise ValueError("No valid price from yfinance")

        change     = round(price - float(prev_close), 2) if prev_close else None
        change_pct = round(change / float(prev_close) * 100, 2) if (change and prev_close) else None

        return {
            "ticker": ticker,
            "price":  round(float(price), 2),
            "session": label,
            "source": "yfinance",
            "change": change,
            "change_pct": change_pct,
            "prev_close": round(float(prev_close), 2) if prev_close else None,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        pass

    # ── Source 3: Polygon.io previous close ──────────────────────────────────
    try:
        from app.market_data.polygon_client import get_previous_close
        q = get_previous_close(ticker)
        if q:
            return {
                "ticker": ticker,
                "price":  q["close"],
                "session": "PREVIOUS_CLOSE",
                "source": "polygon",
                "change": None,
                "change_pct": None,
                "timestamp": datetime.now().isoformat(),
                "note": "Previous close — real-time requires Polygon Starter"
            }
    except Exception:
        pass

    return {
        "ticker": ticker,
        "price":  0.0,
        "session": "UNKNOWN",
        "source": "none",
        "error": "Could not fetch price from any source"
    }


def get_bulk_market_prices(tickers: list[str]) -> dict[str, dict]:
    """Get current prices for multiple tickers efficiently using yfinance batch."""
    results = {}
    try:
        import yfinance as yf
        data = yf.download(
            tickers=" ".join(tickers),
            period="2d",
            progress=False,
            auto_adjust=True
        )
        session = _is_market_hours()

        # For single ticker, yfinance returns different structure
        if len(tickers) == 1:
            ticker = tickers[0].upper()
            closes = data["Close"]
            if len(closes) >= 1:
                price = float(closes.iloc[-1])
                results[ticker] = {
                    "ticker": ticker, "price": round(price, 2),
                    "session": session, "source": "yfinance_bulk"
                }
        else:
            for ticker in tickers:
                t = ticker.upper()
                try:
                    closes = data["Close"][ticker]
                    price  = float(closes.iloc[-1])
                    results[t] = {
                        "ticker": t, "price": round(price, 2),
                        "session": session, "source": "yfinance_bulk"
                    }
                except Exception:
                    results[t] = get_market_price(t)  # fallback to individual
    except Exception:
        # Fall back to individual fetches
        for ticker in tickers:
            results[ticker.upper()] = get_market_price(ticker)

    return results