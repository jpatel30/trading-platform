"""
Broker connector interface (Component C2 — abstraction layer).

All broker connectors (Webull, Robinhood, etc.) implement this interface so
MCP tools and future API endpoints can work with any broker uniformly.

Adding a new broker = add a new XConnector(BrokerConnector) class. No
changes needed to callers (MCP tools, FastAPI endpoints) beyond a
broker_name -> connector class lookup.
"""
from abc import ABC, abstractmethod


class BrokerNotConnectedError(Exception):
    """Raised when a user has no active connection for a given broker.

    Callers (MCP tools, API endpoints) should catch this and prompt the
    user to connect that broker (e.g. "Connect your Webull account").
    """

    def __init__(self, user_id: str, broker_name: str):
        self.user_id = user_id
        self.broker_name = broker_name
        super().__init__(f"User {user_id} has no active '{broker_name}' connection")


class BrokerConnector(ABC):
    """Common interface every broker connector must implement."""

    broker_name: str

    @abstractmethod
    def __init__(self, user_id: str):
        ...

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """Return normalized list of positions (stocks and options)."""
        ...

    @abstractmethod
    def get_balance(self) -> dict:
        """Return account balance/buying power info."""
        ...

    @abstractmethod
    def get_orders(self) -> list[dict]:
        """Return recent/today's orders."""
        ...