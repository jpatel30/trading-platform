"""
Scanner / Discovery Engine (Component C5).

Priority order:
1. Webull live watchlist (via OpenAPI — auto-loads your 127 stocks)
2. Current Webull positions (official SDK)
3. Extra tickers
4. Default universe (top optionable US stocks, only if total < 5)
"""
from app.utils.current_user import get_current_user_id

DEFAULT_UNIVERSE = [
    "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA",
    "AMD","AVGO","INTC","MU","QCOM","ARM","SNDK","WDC",
    "JPM","GS","BAC","V","MA",
    "SPY","QQQ","IWM","GLD","SLV",
    "CEG","ETN","NOW","PLTR","CRM","SNOW",
    "GEV","VRT","CRWD","IBM","MRVL",
]


def get_scan_universe(
    user_id: str | None = None,
    include_positions: bool = True,
    include_watchlist: bool = True,
    filters: dict | None = None,
    extra_tickers: list[str] | None = None,
    max_tickers: int = 150,
) -> list[str]:
    """
    Build the full scan universe for today's analysis.

    Priority:
    1. Webull live watchlist (via OpenAPI — 127 stocks auto-loaded)
    2. Current Webull positions (official SDK)
    3. Extra tickers (caller-provided)
    4. Default universe (only if total < 5)
    """
    if user_id is None:
        try:
            user_id = get_current_user_id()
        except Exception:
            pass

    tickers = []

    # ── Priority 1: Webull live watchlist (OpenAPI) ───────────────────────────
    if include_watchlist:
        try:
            from app.broker.webull_watchlist_api import get_watchlist_tickers
            wl_tickers = get_watchlist_tickers(user_id=user_id)
            if wl_tickers:
                tickers.extend(wl_tickers)
                print(f"[Scanner] Webull watchlist: {len(wl_tickers)} tickers")
            else:
                print("[Scanner] Webull watchlist empty — run setup_token() in webull_watchlist_api")
        except Exception as e:
            print(f"[Scanner] Watchlist unavailable: {e}")

    # ── Priority 2: Current Webull positions ──────────────────────────────────
    if include_positions and user_id:
        try:
            from app.broker.webull_connector import WebullConnector
            wb  = WebullConnector(user_id)
            pos = wb.get_positions()
            pos_tickers = [
                p["symbol"].split()[0]
                for p in pos
                if p.get("instrument_type") == "STOCK"
            ]
            new = [t for t in pos_tickers if t not in tickers]
            if new:
                tickers.extend(new)
                print(f"[Scanner] Positions: {len(new)} new tickers added")
        except Exception as e:
            print(f"[Scanner] Positions unavailable: {e}")

    # ── Priority 3: Extra tickers ─────────────────────────────────────────────
    if extra_tickers:
        for t in extra_tickers:
            t = t.upper().strip()
            if t and t not in tickers:
                tickers.append(t)

    # ── Priority 4: Default universe if still nearly empty ────────────────────
    if len(tickers) < 5:
        filtered = _apply_filters(DEFAULT_UNIVERSE, filters or {})
        for t in filtered:
            if t not in tickers:
                tickers.append(t)
        print(f"[Scanner] Default universe: {len(filtered)} tickers")

    # ── Deduplicate + cap ─────────────────────────────────────────────────────
    seen, result = set(), []
    for t in tickers:
        t = t.upper()
        if t not in seen and len(result) < max_tickers:
            seen.add(t)
            result.append(t)

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