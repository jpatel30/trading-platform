"""
Query functions for the `users` table.
"""
from sqlalchemy import text

from app.db.session import get_session


def get_user_by_email(email: str) -> dict | None:
    """Return {id, email, display_name, is_active} or None."""
    with get_session() as session:
        result = session.execute(
            text("SELECT id, email, display_name, is_active FROM users WHERE email = :email"),
            {"email": email},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    """Return {id, email, display_name, is_active} or None."""
    with get_session() as session:
        result = session.execute(
            text("SELECT id, email, display_name, is_active FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None