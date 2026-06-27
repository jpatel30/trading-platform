"""
Broker Factory — Multi-broker abstraction layer.

Returns the correct broker connector for a user based on their
active broker_connections record.

Currently supported: Webull
Planned: Robinhood, IBKR, Tastytrade

Usage:
    from app.broker.factory import get_broker
    broker = get_broker(user_id)
    positions = broker.get_positions()
"""
from abc import ABC, abstractmethod


# ─────────────────────────────────────────────────────────────────────────────
# Abstract Base
# ─────────────────────────────────────────────────────────────────────────────

class BrokerConnector(ABC):
    """Standard interface all brokers must implement."""

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """Return all open positions."""
        ...

    @abstractmethod
    def get_balances(self) -> dict:
        """Return account balance and buying power."""
        ...

    @abstractmethod
    def get_orders(self) -> list[dict]:
        """Return today's orders."""
        ...

    def get_broker_name(self) -> str:
        return self.__class__.__name__


# ─────────────────────────────────────────────────────────────────────────────
# Webull Wrapper (existing connector behind abstract interface)
# ─────────────────────────────────────────────────────────────────────────────

class WebullBroker(BrokerConnector):
    def __init__(self, user_id: str):
        from app.broker.webull_connector import WebullConnector
        self._conn    = WebullConnector(user_id)
        self.user_id  = user_id

    def get_positions(self) -> list[dict]:
        return self._conn.get_positions()

    def get_balances(self) -> dict:
        try:
            return self._conn.get_balance()
        except Exception:
            return {}

    def get_orders(self) -> list[dict]:
        try:
            return self._conn.get_orders()
        except Exception:
            return []

    def get_broker_name(self) -> str:
        return "webull"


# ─────────────────────────────────────────────────────────────────────────────
# Robinhood Stub (placeholder — implement when users request)
# ─────────────────────────────────────────────────────────────────────────────

class RobinhoodBroker(BrokerConnector):
    def __init__(self, user_id: str):
        raise NotImplementedError(
            "Robinhood connector not yet implemented. "
            "Coming soon — contact support to request priority."
        )

    def get_positions(self): return []
    def get_balances(self):  return {}
    def get_orders(self):    return []


# ─────────────────────────────────────────────────────────────────────────────
# Factory — returns correct broker for user
# ─────────────────────────────────────────────────────────────────────────────

BROKER_MAP = {
    "webull":           WebullBroker,
    "webull_personal":  WebullBroker,
    "webull_market_data": WebullBroker,
    "robinhood":        RobinhoodBroker,
}


def get_broker(user_id: str) -> BrokerConnector:
    """
    Factory: returns the correct broker connector for a user.
    Reads active broker_connections record from DB.
    Falls back to Webull if no connection record found.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text("""
                SELECT broker_name FROM broker_connections
                WHERE user_id = :uid AND is_active = TRUE
                ORDER BY created_at DESC LIMIT 1
            """), {"uid": user_id}).fetchone()

        broker_name = row.broker_name if row else "webull"
        cls = BROKER_MAP.get(broker_name, WebullBroker)
        return cls(user_id)

    except Exception:
        # Default fallback
        return WebullBroker(user_id)


def get_active_broker_name(user_id: str) -> str:
    """Get the name of user's active broker."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text("""
                SELECT broker_name FROM broker_connections
                WHERE user_id = :uid AND is_active = TRUE
                LIMIT 1
            """), {"uid": user_id}).fetchone()
        return row.broker_name if row else "webull"
    except Exception:
        return "webull"


def list_supported_brokers() -> list[dict]:
    """Return list of supported brokers for onboarding UI."""
    return [
        {
            "id":          "webull",
            "name":        "Webull",
            "status":      "supported",
            "description": "Full support — positions, orders, watchlist",
            "auth_method": "api_key",
        },
        {
            "id":          "robinhood",
            "name":        "Robinhood",
            "status":      "coming_soon",
            "description": "Options trading support coming soon",
            "auth_method": "oauth2",
        },
        {
            "id":          "ibkr",
            "name":        "Interactive Brokers",
            "status":      "planned",
            "description": "Professional trading — planned for Q3 2026",
            "auth_method": "api_key",
        },
        {
            "id":          "tastytrade",
            "name":        "Tastytrade",
            "status":      "planned",
            "description": "Options-focused broker — planned",
            "auth_method": "oauth2",
        },
    ]
