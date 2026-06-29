# Predefined ticker universe by sector/cap for no-watchlist users
UNIVERSE = {
    ("tech",       "large"): ["AAPL","MSFT","GOOGL","META","NVDA","AMD","AMZN","NFLX","INTC","QCOM","AVGO","CRM","ORCL","ADBE","TSLA"],
    ("tech",       "mid"):   ["SNOW","AFRM","PINS","PLTR","CRWD","PANW","MDB","COIN","RKLB","ASTS","IONQ","DDOG","ZS","OKTA","NET"],
    ("tech",       "any"):   ["AAPL","MSFT","GOOGL","META","NVDA","AMD","PLTR","CRWD","AFRM","PINS","SNOW","MDB","COIN","NFLX","AVGO"],
    ("energy",     "large"): ["XOM","CVX","COP","EOG","SLB","PSX","VLO","OXY","HAL","MPC"],
    ("energy",     "mid"):   ["RIG","DVN","MRO","APA","CTRA","SM","CHK","AR","EQT","NOG"],
    ("energy",     "any"):   ["XOM","CVX","COP","EOG","SLB","DVN","MRO","OXY","PSX","VLO"],
    ("finance",    "large"): ["JPM","BAC","GS","MS","BLK","V","MA","AXP","C","WFC"],
    ("finance",    "mid"):   ["AFRM","SOFI","UPST","NU","HOOD","PYPL","COIN","LC","OPEN","SQ"],
    ("finance",    "any"):   ["JPM","BAC","GS","V","MA","PYPL","AFRM","SOFI","COIN","HOOD"],
    ("healthcare", "large"): ["JNJ","UNH","PFE","ABBV","LLY","MRK","BMY","AMGN","GILD","CVS"],
    ("healthcare", "mid"):   ["HIMS","TMDX","RXRX","RGEN","NVAX","MRNA","BNTX","BEAM","CRSP","EDIT"],
    ("healthcare", "any"):   ["JNJ","UNH","LLY","ABBV","PFE","HIMS","MRNA","AMGN","GILD","BMY"],
    ("consumer",   "large"): ["AMZN","TSLA","SBUX","NKE","MCD","HD","TGT","WMT","COST","DIS"],
    ("consumer",   "mid"):   ["RBLX","ABNB","LYFT","UBER","DASH","RIVN","LCID","GME","AMC","WYNN"],
    ("consumer",   "any"):   ["AMZN","TSLA","SBUX","NKE","MCD","HD","ABNB","UBER","RBLX","DASH"],
    ("all",        "large"): ["AAPL","MSFT","GOOGL","META","NVDA","AMZN","TSLA","JPM","V","JNJ","XOM","UNH","HD","MA","PG"],
    ("all",        "mid"):   ["PLTR","CRWD","AFRM","SNOW","COIN","HIMS","RKLB","ASTS","SOFI","PINS","MDB","DDOG","NET","ZS","OKTA"],
    ("all",        "any"):   ["AAPL","MSFT","NVDA","TSLA","META","GOOGL","AMZN","PLTR","CRWD","AFRM","SNOW","COIN","HIMS","PINS","AMD"],
}

def get_filtered_universe(sector: str, cap_size: str, catalyst: str, user_id: str = None) -> list[str]:
    """
    Returns focused ticker list (15-40) based on criteria.
    If user has watchlist → filter it by sector/cap.
    If no watchlist → use predefined universe.
    Catalyst filter applied on top.
    """
    sector   = (sector   or "all").lower()
    cap_size = (cap_size or "any").lower()
    catalyst = (catalyst or "any").lower()

    # Try user watchlist first
    base_tickers = []
    if user_id:
        try:
            from sqlalchemy import text
            from app.db.session import get_session
            with get_session() as s:
                rows = s.execute(text(
                    "SELECT ticker FROM user_watchlist WHERE user_id=:uid"
                ), {"uid": user_id}).fetchall()
            base_tickers = [r.ticker for r in rows]
        except Exception:
            pass

    if not base_tickers:
        # No watchlist — use predefined universe
        key = (sector, cap_size)
        base_tickers = UNIVERSE.get(key) or UNIVERSE.get((sector, "any")) or UNIVERSE.get(("all","any"))

    if not base_tickers:
        base_tickers = UNIVERSE[("all","any")]

    # Apply catalyst filter
    if catalyst == "earnings":
        try:
            from app.options_flow.unusual_whales import get_earnings_premarket, get_earnings_afterhours
            earnings_tickers = {
                e.get("symbol","").upper()
                for e in (get_earnings_premarket() or []) + (get_earnings_afterhours() or [])
            }
            earnings_filtered = [t for t in base_tickers if t in earnings_tickers]
            if earnings_filtered:
                return earnings_filtered[:20]
        except Exception:
            pass

    elif catalyst == "momentum":
        # Will be filtered by scanner — just return full list
        pass

    elif catalyst == "breakout":
        # Return tickers near highs — scanner will score them
        pass

    return base_tickers[:40]
