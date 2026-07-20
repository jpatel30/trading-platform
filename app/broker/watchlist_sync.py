"""
Watchlist Sync Service.

Architecture: DB-first with background Webull sync.

Flow:
    1. get_watchlist() → returns from DB instantly (<100ms)
    2. Background thread fetches Webull API (live tickers)
    3. Compares DB vs Webull:
        - DB == Webull (same set) → no action
        - Webull has new tickers → INSERT to DB
        - Webull removed tickers → DELETE from DB
    4. Next call: DB already in sync

Why DB-first:
    - Instant response (no API latency)
    - Works when Webull API is down
    - Enables adding platform-specific metadata (sector, notes, added_at)
    - Background sync keeps it current without blocking user

Tables used:
    user_watchlist (user_id, symbol, source, added_at, notes)
    source: 'webull' | 'manual' | 'scanner' (so we know origin)
"""
import threading
import time
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# DB Operations
# ─────────────────────────────────────────────────────────────────────────────

def get_db_watchlist(user_id: str) -> list[str]:
    """Get tickers from DB cache. Instant."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as session:
            rows = session.execute(
                text("""
                    SELECT ticker FROM user_watchlist
                    WHERE user_id = :uid
                    ORDER BY added_at DESC
                """),
                {"uid": user_id}
            ).fetchall()
            return [r[0] for r in rows]
    except Exception:
        return []


def add_to_db_watchlist(user_id: str, symbols: list[str], source: str = "webull") -> int:
    """Add tickers to DB watchlist. Returns count added."""
    if not symbols:
        return 0
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        added = 0
        with get_session() as session:
            for sym in symbols:
                result = session.execute(
                    text("""
                        INSERT INTO user_watchlist (user_id, ticker, notes, sector)
                        VALUES (:uid, :sym, '', '')
                        ON CONFLICT (user_id, ticker) DO NOTHING
                    """),
                    {"uid": user_id, "sym": sym.upper(), "src": source}
                )
                added += result.rowcount
        return added
    except Exception:
        return 0


def remove_from_db_watchlist(user_id: str, symbols: list[str]) -> int:
    """Remove tickers from DB watchlist. Returns count removed."""
    if not symbols:
        return 0
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        removed = 0
        with get_session() as session:
            for sym in symbols:
                result = session.execute(
                    text("""
                        DELETE FROM user_watchlist
                        WHERE user_id = :uid AND ticker = :sym
                    """),
                    {"uid": user_id, "sym": sym.upper()}
                )
                removed += result.rowcount
        return removed
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Sync Logic
# ─────────────────────────────────────────────────────────────────────────────

_last_sync: dict[str, float] = {}   # user_id → last sync timestamp
SYNC_COOLDOWN = 300                  # don't re-sync more than once per 5 minutes


def sync_with_webull(user_id: str, force: bool = False) -> dict:
    """
    Sync DB watchlist with live Webull API.
    Compares sets: adds new tickers, removes deleted ones.
    No-op if they're identical.

    Returns: {added: [...], removed: [...], unchanged: int}
    """
    # Rate limit: don't hammer Webull API
    now = time.time()
    if not force and (now - _last_sync.get(user_id, 0)) < SYNC_COOLDOWN:
        return {"status": "skipped", "reason": "synced recently"}

    try:
        # Fetch live Webull tickers
        from app.broker.webull_watchlist_api import get_watchlist_tickers
        webull_tickers = set(get_watchlist_tickers(user_id=user_id))

        if not webull_tickers:
            return {"status": "skipped", "reason": "Webull returned empty"}

        # Get current DB tickers
        db_tickers = set(get_db_watchlist(user_id))

        # ADD-ONLY sync. Previously this also deleted anything in the DB
        # that wasn't present in live Webull (to_remove = db - webull) —
        # correct under the old design where Webull was the sole source
        # of truth, but actively wrong now: user_watchlist is the real
        # source of truth (scanning, and every user without a broker,
        # depend on it directly). That old behavior meant any manually
        # added ticker not mirrored in Webull would silently get deleted
        # on the next sync. Webull can still ADD tickers it finds as a
        # convenience — it never removes anything anymore.
        to_add = webull_tickers - db_tickers
        added  = add_to_db_watchlist(user_id, list(to_add), source="webull")

        _last_sync[user_id] = now

        result = {
            "status":    "synced",
            "added":     list(to_add),
            "unchanged": len(webull_tickers & db_tickers),
            "total":     len(db_tickers) + len(to_add),
            "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        if to_add:
            print("[Watchlist] Sync: +{} added from Webull, {} unchanged".format(
                len(to_add), result["unchanged"]))
        else:
            print("[Watchlist] Sync: no new tickers from Webull".format())

        return result

    except Exception as e:
        return {"status": "error", "error": str(e)}


def sync_in_background(user_id: str) -> None:
    """Fire-and-forget background sync. Non-blocking."""
    def _run():
        try:
            sync_with_webull(user_id)
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Points
# ─────────────────────────────────────────────────────────────────────────────

def get_watchlist_fast(user_id: str) -> list[str]:
    """
    Primary entry point. Returns from DB instantly, syncs in background.

    If DB is empty (first run), fetches from Webull synchronously
    and seeds the DB before returning.
    """
    db_tickers = get_db_watchlist(user_id)

    if not db_tickers:
        # First run — seed DB synchronously
        print("[Watchlist] DB empty — seeding from Webull...")
        sync_result = sync_with_webull(user_id, force=True)
        db_tickers  = get_db_watchlist(user_id)
        print("[Watchlist] Seeded {} tickers".format(len(db_tickers)))
    else:
        # DB has data — return immediately, sync in background
        sync_in_background(user_id)

    return db_tickers


def get_watchlist_with_sync_status(user_id: str) -> dict:
    """
    Returns tickers + sync metadata. Used by MCP tool.
    """
    tickers       = get_watchlist_fast(user_id)
    last_sync_ago = int(time.time() - _last_sync.get(user_id, 0))

    return {
        "tickers":         tickers,
        "count":           len(tickers),
        "source":          "DB cache (Webull sync in background)",
        "last_synced_ago": "{} seconds ago".format(last_sync_ago) if last_sync_ago < 3600
                           else "{} minutes ago".format(last_sync_ago // 60),
        "note": "Background sync running — next call will reflect any Webull changes",
    }


def force_sync(user_id: str) -> dict:
    """Force immediate sync regardless of cooldown. Returns diff."""
    print("[Watchlist] Force sync requested...")
    return sync_with_webull(user_id, force=True)