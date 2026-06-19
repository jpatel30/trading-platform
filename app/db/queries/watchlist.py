"""
Query functions for the user_watchlist table.

Schema (add to migrations):
    CREATE TABLE IF NOT EXISTS user_watchlist (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id     UUID NOT NULL REFERENCES users(id),
        ticker      TEXT NOT NULL,
        notes       TEXT,
        min_market_cap  BIGINT,
        sector          TEXT,
        added_at    TIMESTAMPTZ DEFAULT now(),
        UNIQUE(user_id, ticker)
    );
"""
from sqlalchemy import text
from app.db.session import get_session


def get_watchlist(user_id: str) -> list[dict]:
    """Return all tickers in a user's watchlist."""
    with get_session() as session:
        result = session.execute(
            text("""
                SELECT ticker, notes, sector, added_at
                FROM user_watchlist
                WHERE user_id = :user_id
                ORDER BY added_at DESC
            """),
            {"user_id": user_id}
        )
        return [dict(r._mapping) for r in result.fetchall()]


def add_to_watchlist(user_id: str, ticker: str, notes: str = "", sector: str = "") -> bool:
    """Add a ticker to the user's watchlist. Returns True if added, False if already exists."""
    with get_session() as session:
        result = session.execute(
            text("""
                INSERT INTO user_watchlist (user_id, ticker, notes, sector)
                VALUES (:user_id, :ticker, :notes, :sector)
                ON CONFLICT (user_id, ticker) DO NOTHING
                RETURNING id
            """),
            {"user_id": user_id, "ticker": ticker.upper(), "notes": notes, "sector": sector}
        )
        return result.fetchone() is not None


def remove_from_watchlist(user_id: str, ticker: str) -> bool:
    """Remove a ticker from the user's watchlist."""
    with get_session() as session:
        result = session.execute(
            text("""
                DELETE FROM user_watchlist
                WHERE user_id = :user_id AND ticker = :ticker
            """),
            {"user_id": user_id, "ticker": ticker.upper()}
        )
        return result.rowcount > 0


def clear_watchlist(user_id: str) -> int:
    """Clear all tickers from a user's watchlist. Returns count removed."""
    with get_session() as session:
        result = session.execute(
            text("DELETE FROM user_watchlist WHERE user_id = :user_id"),
            {"user_id": user_id}
        )
        return result.rowcount