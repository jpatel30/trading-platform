"""
Query functions for the `user_api_keys` table.
"""
from sqlalchemy import text

from app.db.session import get_session


def get_user_id_for_api_key(api_key_hash: str) -> str | None:
    """
    Return user_id for an active API key hash (and bump last_used_at).
    Returns None if the hash doesn't match any active key.
    """
    with get_session() as session:
        result = session.execute(
            text(
                """
                UPDATE user_api_keys
                SET last_used_at = now()
                WHERE api_key_hash = :hash AND is_active = TRUE
                RETURNING user_id
                """
            ),
            {"hash": api_key_hash},
        )
        row = result.fetchone()
        return str(row[0]) if row else None


def deactivate_active_api_keys(user_id: str) -> int:
    """
    Mark every currently-active API key for this user is_active=FALSE.
    Used by key regeneration — chosen over leaving old keys live
    alongside the new one, since a lost/leaked key that's still valid
    defeats the point of regenerating at all; there's no current use
    case here for a user holding multiple simultaneously-valid keys.
    Returns how many rows were deactivated.
    """
    with get_session() as session:
        result = session.execute(
            text(
                """
                UPDATE user_api_keys SET is_active = FALSE
                WHERE user_id = :user_id AND is_active = TRUE
                """
            ),
            {"user_id": user_id},
        )
        return result.rowcount


def create_api_key(user_id: str, api_key_hash: str, label: str = "primary", scopes: str = '["read","write"]') -> str:
    """Insert a new API key hash for a user. Returns the new key's id."""
    with get_session() as session:
        result = session.execute(
            text(
                """
                INSERT INTO user_api_keys (user_id, api_key_hash, label, scopes, is_active)
                VALUES (:user_id, :hash, :label, CAST(:scopes AS jsonb), TRUE)
                RETURNING id
                """
            ),
            {"user_id": user_id, "hash": api_key_hash, "label": label, "scopes": scopes},
        )
        return str(result.fetchone()[0])