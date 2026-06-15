"""
Webull broker connector (Component C2).

Implements BrokerConnector. Wraps the official Webull SDK with:
- Credential loading (from broker_connections table, encrypted, by user_id)
- Account ID discovery/caching
- Position, balance, and order retrieval

US-specific notes (discovered during integration):
- Auth is App Key + App Secret with HMAC-SHA1 signing (handled by SDK).
- Default endpoint resolution (api.webull.com) is correct - do NOT override.
- IMPORTANT: The entire `webullsdktrade.trade.v2` module (account_info_v2,
  order_operation_v2) is HK/JP-first. Every method's docstring states:
  "not yet available to Webull US brokerage customers, but support will
  be introduced progressively in the future." All v2 calls return 404 for
  US accounts as of June 2026. USE V1 ONLY for US accounts:
    - account.get_app_subscriptions() -> discover account_id
    - account.get_account_position() -> positions
    - account.get_account_balance() -> balance
    - order.list_today_orders() -> today's orders
  Revisit v2 periodically as Webull rolls out US support.
- `get_account_balance` requires a currency code, e.g. 'USD'.

Multi-user notes:
- Each user must register their OWN Webull Developer App (App Key/Secret)
  tied to their own Webull brokerage account + complete their own 2FA
  verification. There is no shared/global credential - if a user hasn't
  connected Webull yet, BrokerNotConnectedError is raised (the UI/MCP
  layer should catch this and prompt "Connect your Webull account").
"""
from webullsdkcore.client import ApiClient
from webullsdkcore.common.region import Region
from webullsdktrade.api import API

from app.broker.base import BrokerConnector, BrokerNotConnectedError
from app.db.queries.broker_connections import get_broker_credentials
from app.utils.crypto import decrypt_token


def _load_credentials_from_db(user_id: str) -> tuple[str, str] | None:
    """Load and decrypt this user's Webull App Key/Secret, if connected."""
    creds = get_broker_credentials(user_id, "webull")
    if not creds:
        return None
    return decrypt_token(creds[0]), decrypt_token(creds[1])


class WebullConnector(BrokerConnector):
    broker_name = "webull"

    def __init__(self, user_id: str):
        self.user_id = user_id

        creds = _load_credentials_from_db(user_id)
        if not creds:
            raise BrokerNotConnectedError(user_id, self.broker_name)
        self.app_key, self.app_secret = creds

        client = ApiClient(self.app_key, self.app_secret, Region.US.value)
        self.api = API(client)
        self._account_id: str | None = None

    def get_account_id(self, force_refresh: bool = False) -> str:
        """Discover and cache the account_id via app subscriptions."""
        if self._account_id and not force_refresh:
            return self._account_id

        res = self.api.account.get_app_subscriptions()
        res.raise_for_status()
        subs = res.json()
        if not subs:
            raise RuntimeError("No app subscriptions found - check Webull app credentials/permissions")

        self._account_id = subs[0]["account_id"]
        return self._account_id

    def get_positions(self, page_size: int = 100) -> list[dict]:
        """
        Fetch all current positions (holdings) for the account.

        The Webull API returns {"has_next": bool, "holdings": [...]}.
        This method unwraps it, paginates via last_instrument_id if needed,
        and normalizes OPTION positions which come back without the
        100x contract multiplier applied to market_value/pnl/pnl_pct.
        """
        account_id = self.get_account_id()
        all_holdings: list[dict] = []
        last_instrument_id = None

        while True:
            res = self.api.account.get_account_position(
                account_id, page_size=page_size, last_instrument_id=last_instrument_id
            )
            res.raise_for_status()
            data = res.json()
            holdings = data.get("holdings", [])
            all_holdings.extend(holdings)

            if not data.get("has_next") or not holdings:
                break
            last_instrument_id = holdings[-1].get("instrument_id")

        for h in all_holdings:
            if h.get("instrument_type") == "OPTION":
                qty = float(h["qty"])
                unit_cost = float(h["unit_cost"])
                last_price = float(h["last_price"])
                multiplier = 100

                total_cost = qty * unit_cost * multiplier
                market_value = qty * last_price * multiplier
                pnl = market_value - total_cost
                pnl_pct = (pnl / total_cost) if total_cost else 0.0

                h["total_cost"] = round(total_cost, 2)
                h["market_value"] = round(market_value, 2)
                h["unrealized_profit_loss"] = round(pnl, 2)
                h["unrealized_profit_loss_rate"] = round(pnl_pct, 4)

        return all_holdings

    def get_balance(self, currency: str = "USD") -> dict:
        """Fetch account balance/buying power."""
        account_id = self.get_account_id()
        res = self.api.account.get_account_balance(account_id, currency)
        res.raise_for_status()
        return res.json()

    def get_orders(self, page_size: int = 100) -> list[dict]:
        """
        Fetch today's orders (combo orders with their fill items).

        Uses the v1 `order.list_today_orders` endpoint - the v2 historical
        endpoint (`order_v2.get_order_history_request`) returns 404 for US
        accounts, same pattern as account_v2.get_account_list().

        Response shape: {"hasNext": bool, "pageSize": int, "orders": [...]}
        Each order is a "combo" with an `items` list containing the actual
        fill/order details (symbol, side, qty, price, status, etc.)
        """
        account_id = self.get_account_id()
        all_orders: list[dict] = []
        last_client_order_id = None

        while True:
            res = self.api.order.list_today_orders(
                account_id, page_size=page_size, last_client_order_id=last_client_order_id
            )
            res.raise_for_status()
            data = res.json()

            orders = data.get("orders", [])
            all_orders.extend(orders)

            if not data.get("hasNext") or not orders:
                break
            last_client_order_id = orders[-1].get("client_order_id")

        return all_orders