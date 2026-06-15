"""
MCP Server — entry point.

Phase 1, Week 1: minimal skeleton with a health-check tool.
Phase 1, Week 2: adds Webull broker tools (get_positions, get_balances).
"""
import sys
from pathlib import Path

# Ensure project root is on sys.path so `app.*` imports work when this
# script is run directly (e.g. by Claude Desktop as a subprocess), not
# just via `python3 -m app.mcp_server.server`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastmcp import FastMCP

from app.db.session import get_session
from app.broker.webull_connector import WebullConnector
from app.broker.base import BrokerNotConnectedError
from app.utils.current_user import get_current_user_id
from sqlalchemy import text

mcp = FastMCP("Personal Trading Intelligence Platform")


@mcp.tool()
def ping() -> str:
    """Health check — confirms the MCP server is running and can reach the database."""
    with get_session() as session:
        result = session.execute(text("SELECT email, display_name FROM users LIMIT 1"))
        row = result.fetchone()
        if row:
            return f"pong — DB connected. User: {row.display_name} ({row.email})"
        return "pong — DB connected but no users found."


@mcp.tool()
def get_positions() -> list[dict]:
    """Fetch current live positions from Webull (stocks and options)."""
    user_id = get_current_user_id()
    try:
        wb = WebullConnector(user_id)
    except BrokerNotConnectedError:
        return {"error": "Webull is not connected for this user. Run app/broker/store_webull_credentials.py first."}
    return wb.get_positions()


@mcp.tool()
def get_balances() -> dict:
    """Fetch current account balance, cash, and buying power from Webull."""
    user_id = get_current_user_id()
    try:
        wb = WebullConnector(user_id)
    except BrokerNotConnectedError:
        return {"error": "Webull is not connected for this user. Run app/broker/store_webull_credentials.py first."}
    return wb.get_balance()


@mcp.tool()
def get_orders() -> list[dict]:
    """Fetch today's orders from Webull, including filled, cancelled, and pending orders with fill details."""
    user_id = get_current_user_id()
    try:
        wb = WebullConnector(user_id)
    except BrokerNotConnectedError:
        return {"error": "Webull is not connected for this user. Run app/broker/store_webull_credentials.py first."}
    return wb.get_orders()


if __name__ == "__main__":
    mcp.run()