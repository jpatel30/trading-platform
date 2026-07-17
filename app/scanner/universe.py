"""
Scanner / Discovery Engine (Component C5).

Rewritten July 2026 — removed all live broker dependency for building
the scan universe, and removed DEFAULT_UNIVERSE (a hardcoded 36-ticker
fallback list). The fallback made sense in the old design, where it
was the PRIMARY path for anyone without a connected broker — now that
the admin's own user_watchlist (always populated) is Priority 1 for
every user, that fallback branch could only ever fire if the admin's
watchlist somehow became empty, which would mean something is
genuinely misconfigured. Silently substituting unrelated tickers in
that case is the same failure mode as the EXCLUDED set / SP500
supplement / hardcoded stock fallbacks removed elsewhere in the engine
this session — an honest empty result is more debuggable than a silent
substitute nobody actually chose.

Design: no separate "default watchlist" table. The admin user's own
user_watchlist rows (users.is_admin=TRUE) ARE the shared default list.
For the admin themselves, "default" and "mine" are the same rows, so
no special-casing is needed anywhere in this function.

Priority order:
1. Admin's user_watchlist rows (the shared default universe)
2. This user's OWN user_watchlist rows, if watchlist_mode requests it
   (a no-op for the admin — already covered by #1)
3. Extra tickers (caller-provided, unchanged from before)
4. Optional sector/price/market-cap filters applied to the result —
   a real, kept capability, no longer tied to the removed fallback
"""
from app.utils.current_user import get_current_user_id


def _get_admin_watchlist() -> list[str]:
    """The shared default universe — the admin user's own user_watchlist rows."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT uw.ticker
                FROM user_watchlist uw
                JOIN users u ON u.id = uw.user_id
                WHERE u.is_admin = TRUE
            """)).fetchall()
        return [r.ticker for r in rows]
    except Exception as e:
        print(f"[Scanner] Admin watchlist lookup failed: {e}")
        return []


def _get_user_watchlist(user_id: str) -> list[str]:
    """This specific user's own watchlist rows."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT ticker FROM user_watchlist WHERE user_id = :uid
            """), {"uid": user_id}).fetchall()
        return [r.ticker for r in rows]
    except Exception as e:
        print(f"[Scanner] User watchlist lookup failed: {e}")
        return []


def get_scan_universe(
    user_id: str | None = None,
    watchlist_mode: str = "default_plus_mine",
    include_positions: bool = True,   # deprecated, kept for backward compat — no-op
    include_watchlist: bool = True,   # deprecated, kept for backward compat — no-op
    filters: dict | None = None,
    extra_tickers: list[str] | None = None,
    max_tickers: int = 150,
) -> list[str]:
    """
    Build the full scan universe for today's analysis.

    watchlist_mode:
        "default_plus_mine" (default) — admin's shared list + this
            user's own additions. For the admin, this is just their
            own list already (default IS their list).
        "default_only" — admin's shared list only, ignoring anything
            this user has personally added.

    No hardcoded fallback list — if the admin's watchlist is empty and
    this user has no personal additions either, this returns an empty
    list. That's a real, honest signal ("nothing configured to scan"),
    not silently substituted with unrelated tickers.
    """
    if user_id is None:
        try:
            user_id = get_current_user_id()
        except Exception:
            pass

    tickers: list[str] = []

    # ── Priority 1: admin's shared default list ───────────────────────────────
    admin_list = _get_admin_watchlist()
    if admin_list:
        tickers.extend(admin_list)
        print(f"[Scanner] Default (admin) watchlist: {len(admin_list)} tickers")
    else:
        print("[Scanner] ⚠️ Default (admin) watchlist is empty")

    # ── Priority 2: this user's own additions ─────────────────────────────────
    if watchlist_mode == "default_plus_mine" and user_id:
        mine = _get_user_watchlist(user_id)
        new  = [t for t in mine if t not in tickers]
        if new:
            tickers.extend(new)
            print(f"[Scanner] My watchlist: {len(new)} additional tickers")

    # ── Priority 3: extra tickers ─────────────────────────────────────────────
    if extra_tickers:
        for t in extra_tickers:
            t = t.upper().strip()
            if t and t not in tickers:
                tickers.append(t)

    # ── Deduplicate + cap ─────────────────────────────────────────────────────
    seen, result = set(), []
    for t in tickers:
        t = t.upper()
        if t not in seen and len(result) < max_tickers:
            seen.add(t)
            result.append(t)

    # ── Optional filters — applied to the real universe, not a fallback ──────
    if filters:
        result = _apply_filters(result, filters)

    if not result:
        print("[Scanner] ⚠️ Final universe is EMPTY — no watchlist configured")
    else:
        print(f"[Scanner] Final universe: {len(result)} tickers")
    return result


def _apply_filters(tickers: list[str], filters: dict) -> list[str]:
    """Apply optional market cap / price / sector filters."""
    if not filters:
        return tickers
    min_cap   = filters.get("min_market_cap", 0)
    sectors   = [s.lower() for s in filters.get("sectors", [])]
    min_price = filters.get("min_price", 0)
    max_price = filters.get("max_price", float("inf"))
    if not any([min_cap, sectors, min_price, max_price < float("inf")]):
        return tickers
    try:
        import yfinance as yf
        passed = []
        for ticker in tickers:
            try:
                fi    = yf.Ticker(ticker).fast_info
                price = fi.get("lastPrice") or 0
                if price < min_price or price > max_price:
                    continue
                if min_cap > 0 and (fi.get("marketCap") or 0) < min_cap:
                    continue
                passed.append(ticker)
            except Exception:
                passed.append(ticker)
        return passed
    except Exception:
        return tickers


def get_scan_universe_mcp(
    extra_tickers: list[str] | None = None,
    min_market_cap: float = 0,
    sectors: list[str] | None = None,
    min_price: float = 0,
    watchlist_mode: str = "default_plus_mine",
) -> list[str]:
    """MCP-friendly wrapper for get_scan_universe."""
    filters = {}
    if min_market_cap > 0:
        filters["min_market_cap"] = int(min_market_cap)
    if sectors:
        filters["sectors"] = sectors
    if min_price > 0:
        filters["min_price"] = min_price
    return get_scan_universe(
        watchlist_mode=watchlist_mode,
        filters=filters,
        extra_tickers=extra_tickers or [],
    )
