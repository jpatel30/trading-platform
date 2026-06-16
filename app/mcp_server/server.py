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


if __name__ == "__main__":
    mcp.run()