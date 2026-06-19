"""
Scanner / Discovery Engine (Component C5).

Builds the universe of tickers to analyze each day.

Priority order:
1. User's watchlist (Postgres user_watchlist table)
   — stocks the user is actively watching/tracking

2. User's current Webull positions
   — always analyze what you already own

3. Filter-based discovery (when watchlist is empty or user wants more)
   — filter by market cap, sector, min price, options liquidity
   — uses Polygon.io ticker list + yfinance for fundamentals

4. Manual input (user provides specific tickers)

Output: deduplicated list of tickers ready for TA + flow analysis.

Usage:
    from app.scanner.universe import get_scan_universe

    tickers = get_scan_universe(
        user_id='...',
        include_positions=True,
        include_watchlist=True,
        filters={
            'min_market_cap': 10_000_000_000,  # $10B+
            'sectors': ['Technology', 'Semiconductors'],
            'min_price': 20,
            'max_price': 1000,
        },
        extra_tickers=['NVDA', 'AMD', 'SPY'],
    )
"""
from app.utils.current_user import get_current_user_id


# ── Default scan universe (top optionable US stocks) ─────────────────────────
# Used when user has no watchlist and no filters specified.
# These are the most liquid, actively-traded options names.
DEFAULT_UNIVERSE = [
    # Mega-cap tech
    "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA",
    # Semiconductors
    "AMD","AVGO","INTC","MU","QCOM","ARM","SNDK","WDC",
    # Finance
    "JPM","GS","BAC","V","MA",
    # ETFs (always include)
    "SPY","QQQ","IWM","GLD","SLV",
    # Energy
    "CEG","ETN",
    # Cloud/SaaS
    "NOW","PLTR","CRM","SNOW",
    # Others with high options volume
    "GEV","VRT","CRWD","IBM","MRVL",
]


def get_scan_universe(
    user_id: str | None = None,
    include_positions: bool = True,
    include_watchlist: bool = True,
    filters: dict | None = None,
    extra_tickers: list[str] | None = None,
    max_tickers: int = 50,
) -> list[str]:
    """
    Build the full scan universe for today's analysis run.

    Args:
        user_id:           user ID (auto-resolved from MCP_API_KEY if None)
        include_positions: add tickers from current Webull positions
        include_watchlist: add tickers from user's DB watchlist
        filters:           dict with optional keys:
                           - min_market_cap (int, dollars)
                           - max_market_cap (int, dollars)
                           - sectors (list[str])
                           - min_price (float)
                           - max_price (float)
                           - min_options_volume (int)
        extra_tickers:     additional tickers to always include
        max_tickers:       cap on total tickers returned (default 50)

    Returns:
        Deduplicated list of tickers, watchlist + positions first.
    """
    if user_id is None:
        try:
            user_id = get_current_user_id()
        except Exception:
            pass

    tickers = []

    # ── Priority 1: User watchlist ────────────────────────────────────────────
    if include_watchlist and user_id:
        try:
            from app.db.queries.watchlist import get_watchlist
            wl = get_watchlist(user_id)
            wl_tickers = [r["ticker"] for r in wl]
            tickers.extend(wl_tickers)
            print(f"[Scanner] Watchlist: {len(wl_tickers)} tickers")
        except Exception as e:
            print(f"[Scanner] Watchlist unavailable: {e}")

    # ── Priority 2: Current Webull positions ──────────────────────────────────
    if include_positions and user_id:
        try:
            from app.broker.webull_connector import WebullConnector
            from app.broker.base import BrokerNotConnectedError
            wb  = WebullConnector(user_id)
            pos = wb.get_positions()
            pos_tickers = [
                p["symbol"].split()[0]   # handle "NVDA 230120C..." options
                for p in pos
                if p.get("instrument_type") == "STOCK"
            ]
            new = [t for t in pos_tickers if t not in tickers]
            tickers.extend(new)
            print(f"[Scanner] Positions: {len(pos_tickers)} tickers ({len(new)} new)")
        except Exception as e:
            print(f"[Scanner] Positions unavailable: {e}")

    # ── Priority 3: Extra tickers ─────────────────────────────────────────────
    if extra_tickers:
        for t in extra_tickers:
            t = t.upper().strip()
            if t and t not in tickers:
                tickers.append(t)

    # ── Priority 4: Filter-based discovery ───────────────────────────────────
    # If watchlist is empty AND no extra tickers, use filtered default universe
    if len(tickers) < 5:
        filtered = _apply_filters(DEFAULT_UNIVERSE, filters or {})
        for t in filtered:
            if t not in tickers:
                tickers.append(t)
        print(f"[Scanner] Default universe (filtered): {len(filtered)} tickers")

    # ── Deduplicate + cap ─────────────────────────────────────────────────────
    seen = set()
    result = []
    for t in tickers:
        if t not in seen and len(result) < max_tickers:
            seen.add(t)
            result.append(t.upper())

    print(f"[Scanner] Final universe: {len(result)} tickers")
    return result


def _apply_filters(tickers: list[str], filters: dict) -> list[str]:
    """
    Apply market cap / price / sector filters.
    Uses yfinance for fast bulk fundamentals check.
    """
    if not filters:
        return tickers

    min_cap    = filters.get("min_market_cap", 0)
    max_cap    = filters.get("max_market_cap", float("inf"))
    sectors    = [s.lower() for s in filters.get("sectors", [])]
    min_price  = filters.get("min_price", 0)
    max_price  = filters.get("max_price", float("inf"))

    if not any([min_cap, max_cap < float("inf"), sectors, min_price, max_price < float("inf")]):
        return tickers  # no filters applied

    try:
        import yfinance as yf
        passed = []
        # Batch download basic info — fast
        data = yf.download(
            " ".join(tickers), period="1d", progress=False, auto_adjust=True
        )
        last_prices = {}
        if "Close" in data:
            for t in tickers:
                try:
                    last_prices[t] = float(data["Close"][t].iloc[-1])
                except Exception:
                    last_prices[t] = 0

        for ticker in tickers:
            price = last_prices.get(ticker, 0)

            # Price filter (fast)
            if price < min_price or price > max_price:
                continue

            # Market cap + sector filter (requires individual info call — slower)
            if min_cap > 0 or sectors:
                try:
                    info   = yf.Ticker(ticker).fast_info
                    cap    = info.get("marketCap") or 0
                    if cap < min_cap or cap > max_cap:
                        continue
                    if sectors:
                        sector = (info.get("sector") or "").lower()
                        if not any(s in sector for s in sectors):
                            continue
                except Exception:
                    pass  # include if can't verify

            passed.append(ticker)

        return passed

    except Exception:
        return tickers  # return unfiltered on error


def get_scan_universe_mcp(
    extra_tickers: list[str] | None = None,
    min_market_cap: float = 0,
    sectors: list[str] | None = None,
    min_price: float = 0,
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
        include_positions=True,
        include_watchlist=True,
        filters=filters,
        extra_tickers=extra_tickers or [],
    )