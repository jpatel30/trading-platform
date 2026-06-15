"""
CLI script: connect a broker for a user (store encrypted credentials).

Generic across any broker authenticating via an (app_key, app_secret) /
(access_token, refresh_token)-shaped credential pair - currently: Webull.
Each user generates their own credentials (e.g. Webull Developer Portal
App Key/Secret tied to their own brokerage account + 2FA) and registers
them here.

Brokers with a fundamentally different credential shape (e.g. SnapTrade's
snaptrade_user_id/snaptrade_user_secret for Robinhood, per blueprint
Section 6 broker_connections schema) will need their own connect script -
this one covers the access_token/refresh_token pair columns.

Usage:
    python3 -m app.broker.connect_broker <email> <broker_name> <app_key> <app_secret>

Example:
    python3 -m app.broker.connect_broker jaimin@example.com webull AKxxxx SKxxxx

(Phase 4+ note: the FastAPI "Connect Broker" endpoint will call
 connect_broker() directly instead of running this as a CLI.)
"""
import sys

from app.db.queries.broker_connections import upsert_broker_credentials
from app.db.queries.users import get_user_by_email
from app.utils.crypto import encrypt_token


def connect_broker(email: str, broker_name: str, app_key: str, app_secret: str) -> str | None:
    """Encrypt + store (app_key, app_secret) for this user/broker. Returns connection id or None if user not found."""
    user = get_user_by_email(email)
    if not user:
        return None

    return upsert_broker_credentials(
        user_id=user["id"],
        broker_name=broker_name,
        encrypted_access_token=encrypt_token(app_key),
        encrypted_refresh_token=encrypt_token(app_secret),
    )


def main():
    if len(sys.argv) != 5:
        print("Usage: python3 -m app.broker.connect_broker <email> <broker_name> <app_key> <app_secret>")
        sys.exit(1)

    email, broker_name, app_key, app_secret = sys.argv[1:5]
    conn_id = connect_broker(email, broker_name, app_key, app_secret)
    if conn_id:
        print(f"Connected '{broker_name}' for {email} -> broker_connection id: {conn_id}")
    else:
        print(f"No user found with email {email} - create the user first.")


if __name__ == "__main__":
    main()