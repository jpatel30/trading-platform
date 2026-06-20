"""
Query functions for the `broker_connections` table.

Credentials are stored as encrypted bytes (BYTEA columns access_token /
refresh_token). Encryption/decryption happens in the CALLER (broker
connectors, connect_broker script) - this module only persists/retrieves
raw encrypted bytes, keeping it broker-agnostic.
"""
from sqlalchemy import text

from app.db.session import get_session


def get_broker_credentials(user_id: str, broker_name: str) -> tuple[bytes, bytes] | None:
    """Return (encrypted_access_token, encrypted_refresh_token) for an active connection, or None."""
    with get_session() as session:
        result = session.execute(
            text(
                """
                SELECT access_token, refresh_token
                FROM broker_connections
                WHERE user_id = :user_id AND broker_name = :broker_name AND is_active = TRUE
                """
            ),
            {"user_id": user_id, "broker_name": broker_name},
        )
        row = result.fetchone()
        return (row[0], row[1]) if row else None


def upsert_broker_credentials(
    user_id: str,
    broker_name: str,
    encrypted_access_token: bytes,
    encrypted_refresh_token: bytes,
    auth_method: str = "oauth2",
) -> str | None:
    """Insert or update a broker connection's encrypted credentials. Returns connection id."""
    with get_session() as session:
        result = session.execute(
            text(
                """
                INSERT INTO broker_connections
                    (user_id, broker_name, auth_method, access_token, refresh_token, is_active, last_synced_at)
                VALUES (:user_id, :broker_name, :auth_method, :access_token, :refresh_token, TRUE, now())
                ON CONFLICT (user_id, broker_name)
                DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    auth_method = EXCLUDED.auth_method,
                    is_active = TRUE,
                    last_synced_at = now(),
                    updated_at = now()
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "broker_name": broker_name,
                "auth_method": auth_method,
                "access_token": encrypted_access_token,
                "refresh_token": encrypted_refresh_token,
            },
        )
        row = result.fetchone()
        return str(row[0]) if row else None


def deactivate_broker_connection(user_id: str, broker_name: str) -> bool:
    """Mark a broker connection inactive (user disconnects). Returns True if a row was updated."""
    with get_session() as session:
        result = session.execute(
            text(
                """
                UPDATE broker_connections
                SET is_active = FALSE, updated_at = now()
                WHERE user_id = :user_id AND broker_name = :broker_name
                """
            ),
            {"user_id": user_id, "broker_name": broker_name},
        )
        return result.rowcount > 0


def list_broker_connections(user_id: str) -> list[dict]:
    """List all broker connections for a user (for a 'connected accounts' UI page)."""
    with get_session() as session:
        result = session.execute(
            text(
                """
                SELECT broker_name, auth_method, is_active, last_synced_at, created_at
                FROM broker_connections
                WHERE user_id = :user_id
                """
            ),
            {"user_id": user_id},
        )
        return [dict(row._mapping) for row in result.fetchall()]


"""
Add to app/db/queries/broker_connections.py
"""

def get_all_user_brokers(user_id: str) -> list[str]:
    """Return list of broker names the user has connected."""
    with get_session() as session:
        result = session.execute(
            text("""
                 SELECT DISTINCT broker_name
                 FROM broker_connections
                 WHERE user_id = :user_id
                   AND is_active = TRUE
                 ORDER BY broker_name
                 """),
            {"user_id": user_id}
        )
        return [r[0] for r in result.fetchall()]
