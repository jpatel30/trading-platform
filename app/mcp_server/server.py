"""
MCP Server — entry point.

Phase 1 Week 1-2: Webull broker tools (ping, get_positions, get_balances, get_orders)
Phase 1 Week 3:   Market data tools (get_quote, get_quotes, get_price_history, get_ticker_info)
Phase 1 Week 4:   Technical analysis (analyze_ticker)
Phase 2+:         Options flow, scanner, strategy engine (coming soon)
"""
import sys
from pathlib import Path

# Ensure project root is on sys.path so `app.*` imports work when this
# script is run directly (e.g. by Claude Desktop as a subprocess), not
# just via `python3 -m app.mcp_server.server`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timedelta

from fastmcp import FastMCP
from sqlalchemy import text

from app.broker.base import BrokerNotConnectedError
from app.broker.webull_connector import WebullConnector
from app.db.session import get_session
from app.market_data.polygon_client import (
    get_bars,
    get_bulk_previous_close,
    get_previous_close,
    get_ticker_details,
)
from app.technical_analysis.engine import get_technical_profile
from app.utils.current_user import get_current_user_id

mcp = FastMCP("Personal Trading Intelligence Platform")


# ============================================================
# HEALTH CHECK
# ============================================================

@mcp.tool()
def ping() -> str:
    """Health check — confirms the MCP server is running and can reach the database."""
    with get_session() as session:
        result = session.execute(text("SELECT email, display_name FROM users LIMIT 1"))
        row = result.fetchone()
        if row:
            return f"pong — DB connected. User: {row.display_name} ({row.email})"
        return "pong — DB connected but no users found."


# ============================================================
# WEBULL BROKER TOOLS
# ============================================================

@mcp.tool()
def get_positions() -> list[dict]:
    """
    Fetch current live positions from Webull (stocks and options).
    Options positions are normalized with the 100x contract multiplier applied.
    Returns list of {symbol, instrument_type, qty, unit_cost, last_price,
    market_value, total_cost, unrealized_profit_loss, unrealized_profit_loss_rate}.
    """
    user_id = get_current_user_id()
    try:
        wb = WebullConnector(user_id)
    except BrokerNotConnectedError:
        return [{"error": "Webull is not connected. Run: python3 -m app.broker.connect_broker <email> webull <key> <secret>"}]
    return wb.get_positions()


@mcp.tool()
def get_balances() -> dict:
    """
    Fetch current account balance, cash, and buying power from Webull.
    Returns {account_id, total_asset_currency, total_market_value,
    total_cash_balance, margin_utilization_rate, account_currency_assets}.
    """
    user_id = get_current_user_id()
    try:
        wb = WebullConnector(user_id)
    except BrokerNotConnectedError:
        return {"error": "Webull is not connected. Run: python3 -m app.broker.connect_broker <email> webull <key> <secret>"}
    return wb.get_balance()


@mcp.tool()
def get_orders() -> list[dict]:
    """
    Fetch today's orders from Webull.
    Returns list of combo orders, each with an 'items' list containing
    {symbol, side, order_status, filled_price, qty, order_type, place_time}.
    """
    user_id = get_current_user_id()
    try:
        wb = WebullConnector(user_id)
    except BrokerNotConnectedError:
        return [{"error": "Webull is not connected. Run: python3 -m app.broker.connect_broker <email> webull <key> <secret>"}]
    return wb.get_orders()


# ============================================================
# MARKET DATA TOOLS (Polygon.io)
# ============================================================

@mcp.tool()
def get_quote(ticker: str) -> dict | None:
    """
    Get the previous trading day's OHLCV quote for a ticker.
    Returns {ticker, open, high, low, close, volume, vwap, timestamp, source}.
    """
    return get_previous_close(ticker.upper())


@mcp.tool()
def get_quotes(tickers: list[str]) -> dict[str, dict]:
    """
    Get previous day's OHLCV quotes for multiple tickers at once (cached).
    Returns {ticker: {open, high, low, close, volume, vwap, ...}}
    """
    return get_bulk_previous_close([t.upper() for t in tickers])


@mcp.tool()
def get_price_history(
    ticker: str,
    days: int = 200,
    timespan: str = "day",
) -> list[dict]:
    """
    Get historical OHLCV bars for a ticker.

    Args:
        ticker:   e.g. 'NVDA'
        days:     how many calendar days back to fetch (default 200)
        timespan: 'minute' | 'hour' | 'day' | 'week' | 'month'

    Returns list of {timestamp, open, high, low, close, volume, vwap}
    sorted oldest to newest — ready for technical analysis.
    """
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")
    return get_bars(ticker.upper(), 1, timespan, from_date, to_date)


@mcp.tool()
def get_ticker_info(ticker: str) -> dict | None:
    """
    Get fundamental details for a ticker.
    Returns {ticker, name, market_cap, primary_exchange, type, description}.
    """
    return get_ticker_details(ticker.upper())


# ============================================================
# TECHNICAL ANALYSIS TOOLS (Component C6)
# ============================================================

@mcp.tool()
def analyze_ticker(ticker: str, days: int = 300) -> dict:
    """
    Full technical analysis for a ticker.

    Computes: MA20/50/200, EMA20, RSI(14), MACD(12,26,9),
    Bollinger Bands(20,2), ATR(14), relative volume,
    support/resistance levels, trend direction,
    signal (BUY/SELL/NEUTRAL), and strength score (0-100).

    Args:
        ticker: e.g. 'NVDA'
        days:   calendar days of history to analyze (default 300 for MA200)

    Returns complete technical profile with plain-English summary.
    """
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")
    bars = get_bars(ticker.upper(), 1, "day", from_date, to_date)
    return get_technical_profile(ticker.upper(), bars)


# ============================================================
# OPTIONS FLOW TOOLS (Component C4 — Unusual Whales)
# ============================================================

@mcp.tool()
def get_market_overview() -> dict:
    """
    Full market-wide options flow overview.
    Returns market tide (net call/put premium), total options volume,
    sector ETF flow, and upcoming economic calendar events.
    Tells you the macro direction of institutional money right now.
    """
    from app.options_flow.unusual_whales import (
        get_market_tide, get_total_options_volume,
        get_sector_etfs, get_economic_calendar,
    )
    from app.options_flow.signals import score_market_tide

    tide = get_market_tide()
    tide_score = score_market_tide(tide)
    total_vol = get_total_options_volume()
    sectors = get_sector_etfs()
    econ = get_economic_calendar()

    # Top bullish/bearish sectors
    bullish_sectors = sorted(
        [s for s in sectors if float(s.get("bullish_premium") or 0) > float(s.get("bearish_premium") or 0)],
        key=lambda x: float(x.get("bullish_premium") or 0), reverse=True
    )[:3]
    bearish_sectors = sorted(
        [s for s in sectors if float(s.get("bearish_premium") or 0) > float(s.get("bullish_premium") or 0)],
        key=lambda x: float(x.get("bearish_premium") or 0), reverse=True
    )[:3]

    # Upcoming events in next 7 days
    upcoming_events = econ[:10]

    return {
        "market_tide": tide_score,
        "total_options_volume": total_vol,
        "bullish_sectors": [{"ticker": s["ticker"], "name": s.get("full_name"), "bullish_premium": s.get("bullish_premium")} for s in bullish_sectors],
        "bearish_sectors": [{"ticker": s["ticker"], "name": s.get("full_name"), "bearish_premium": s.get("bearish_premium")} for s in bearish_sectors],
        "upcoming_economic_events": upcoming_events,
    }


@mcp.tool()
def get_options_flow(ticker: str | None = None, min_premium: float = 500000, sweeps_only: bool = False) -> list[dict]:
    """
    Recent options flow alerts (institutional sweep activity).
    Each alert shows: ticker, type (call/put), total_premium, has_sweep,
    volume, open_interest, volume_oi_ratio, strike, expiry, sector.

    Args:
        ticker:       filter to specific ticker (None = all market)
        min_premium:  minimum total premium in $ (default $500K)
        sweeps_only:  if True, only sweeps (multi-exchange aggressive buys)
    """
    from app.options_flow.unusual_whales import get_flow_alerts
    return get_flow_alerts(ticker=ticker, min_premium=min_premium, sweeps_only=sweeps_only)


@mcp.tool()
def get_dark_pool(ticker: str | None = None, min_premium: float = 0) -> list[dict]:
    """
    Recent dark pool (off-exchange institutional block) trades.
    Each trade shows: ticker, premium, size, price, executed_at,
    nbbo_ask, nbbo_bid (compare price to these to infer direction).

    Args:
        ticker:       specific ticker (None = all recent market dark pool)
        min_premium:  minimum trade premium in $ (e.g. 2000000 = $2M+)
    """
    from app.options_flow.unusual_whales import get_dark_pool_ticker, get_dark_pool_recent
    if ticker:
        return get_dark_pool_ticker(ticker.upper())
    return get_dark_pool_recent(min_premium=min_premium)


@mcp.tool()
def get_gex(ticker: str) -> dict:
    """
    Dealer Gamma Exposure (GEX) analysis for a ticker.
    Returns: net_gamma (pos=stabilizing, neg=volatile), gamma_wall (key price level),
    GEX by strike (shows where dealers must hedge), and GEX by expiry.

    The gamma wall is the strongest gravitational price level.
    Negative GEX below current price = downside acceleration zone.
    """
    from app.options_flow.unusual_whales import get_greek_exposure, get_gex_by_strike, get_gex_by_expiry
    from app.options_flow.signals import score_gex
    from app.market_data.polygon_client import get_previous_close

    gex = get_greek_exposure(ticker)
    by_strike = get_gex_by_strike(ticker)
    by_expiry = get_gex_by_expiry(ticker)
    quote = get_previous_close(ticker.upper())
    current_price = quote["close"] if quote else 0

    scored = score_gex(gex, by_strike, current_price)

    # Top 5 strikes with most GEX (gamma walls)
    top_strikes = sorted(
        [s for s in by_strike if s.get("call_gex") or s.get("put_gex")],
        key=lambda x: abs(float(x.get("call_gex") or 0) + float(x.get("put_gex") or 0)),
        reverse=True
    )[:5]

    return {
        "ticker": ticker.upper(),
        "current_price": current_price,
        "gex_score": scored,
        "top_gex_strikes": top_strikes,
        "gex_by_expiry": by_expiry[:10],
    }


@mcp.tool()
def get_ticker_signal(ticker: str) -> dict:
    """
    Complete options flow signal for a ticker — the primary input to the Strategy Engine.

    Combines: options flow sweeps, dark pool, GEX, market tide, earnings risk.
    Returns: direction (BULLISH/BEARISH/NEUTRAL), confidence (0-100),
    signal (STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL/BLOCKED),
    and plain-English summary of all signals.

    This is the tool to call before get_strategy_recommendation().
    """
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package

    pkg = get_signal_package(ticker.upper())
    return score_signal_package(pkg)


@mcp.tool()
def get_earnings_calendar() -> dict:
    """
    Today's earnings reports (pre-market and after-hours).
    Critical for Rule 2: avoid new options positions within 7 days of earnings.
    Returns lists of companies reporting today with expected move percentages.
    """
    from app.options_flow.unusual_whales import get_earnings_afterhours, get_earnings_premarket
    return {
        "premarket": get_earnings_premarket(),
        "afterhours": get_earnings_afterhours(),
    }


@mcp.tool()
def get_news(ticker: str | None = None) -> list[dict]:
    """
    Recent market news headlines with sentiment scoring.
    Each item: headline, source, created_at, tickers, is_major, sentiment.

    Args:
        ticker: filter to ticker-specific news (None = all major headlines)
    """
    from app.options_flow.unusual_whales import get_news_headlines
    return get_news_headlines(ticker=ticker)


@mcp.tool()
def get_congress_trades(ticker: str | None = None) -> list[dict]:
    """
    Recent congressional stock trades (senators + representatives).
    Each trade: name, ticker, txn_type (buy/sell), amounts, transaction_date.
    Congressional buys have historically preceded major price moves.

    Args:
        ticker: filter to specific ticker (None = all recent congress trades)
    """
    from app.options_flow.unusual_whales import get_congress_trades as _get_congress
    return _get_congress(ticker=ticker)


if __name__ == "__main__":
    mcp.run()