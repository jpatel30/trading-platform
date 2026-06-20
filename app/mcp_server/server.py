"""
MCP Server — entry point.

Phase 1 Week 1-2: Webull broker tools (ping, get_positions, get_balances, get_orders)
Phase 1 Week 3:   Market data tools (get_quote, get_quotes, get_price_history, get_ticker_info)
Phase 1 Week 4:   Technical analysis (analyze_ticker)
Phase 2+:         Options flow, scanner, strategy engine (coming soon)
"""
import sys
from pathlib import Path

# Ensure project root is on sys.path so `app.*` imports work when this
# script is run directly (e.g. by Claude Desktop as a subprocess), not
# just via `python3 -m app.mcp_server.server`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timedelta

from fastmcp import FastMCP
from sqlalchemy import text

from app.broker.base import BrokerNotConnectedError
from app.broker.webull_connector import WebullConnector
from app.db.session import get_session
from app.market_data.polygon_client import (
    get_bars,
    get_bulk_previous_close,
    get_previous_close,
    get_ticker_details,
)
from app.technical_analysis.engine import get_technical_profile
from app.utils.current_user import get_current_user_id
mcp = FastMCP("Personal Trading Intelligence Platform")


# ============================================================
# HEALTH CHECK
# ============================================================

@mcp.tool()
def ping() -> str:
    """Health check — confirms the MCP server is running and can reach the database."""
    with get_session() as session:
        result = session.execute(text("SELECT email, display_name FROM users LIMIT 1"))
        row = result.fetchone()
        if row:
            return f"pong — DB connected. User: {row.display_name} ({row.email})"
        return "pong — DB connected but no users found."


# ============================================================
# WEBULL BROKER TOOLS
# ============================================================

@mcp.tool()
def get_positions() -> list[dict]:
    """
    Fetch current live positions from Webull (stocks and options).
    Options positions are normalized with the 100x contract multiplier applied.
    Returns list of {symbol, instrument_type, qty, unit_cost, last_price,
    market_value, total_cost, unrealized_profit_loss, unrealized_profit_loss_rate}.
    """
    user_id = get_current_user_id()
    try:
        wb = WebullConnector(user_id)
    except BrokerNotConnectedError:
        return [{"error": "Webull is not connected. Run: python3 -m app.broker.connect_broker <email> webull <key> <secret>"}]
    return wb.get_positions()


@mcp.tool()
def get_balances() -> dict:
    """
    Fetch current account balance, cash, and buying power from Webull.
    Returns {account_id, total_asset_currency, total_market_value,
    total_cash_balance, margin_utilization_rate, account_currency_assets}.
    """
    user_id = get_current_user_id()
    try:
        wb = WebullConnector(user_id)
    except BrokerNotConnectedError:
        return {"error": "Webull is not connected. Run: python3 -m app.broker.connect_broker <email> webull <key> <secret>"}
    return wb.get_balance()


@mcp.tool()
def get_orders() -> list[dict]:
    """
    Fetch today's orders from Webull.
    Returns list of combo orders, each with an 'items' list containing
    {symbol, side, order_status, filled_price, qty, order_type, place_time}.
    """
    user_id = get_current_user_id()
    try:
        wb = WebullConnector(user_id)
    except BrokerNotConnectedError:
        return [{"error": "Webull is not connected. Run: python3 -m app.broker.connect_broker <email> webull <key> <secret>"}]
    return wb.get_orders()


# ============================================================
# MARKET DATA TOOLS (Polygon.io)
# ============================================================

@mcp.tool()
def get_quote(ticker: str) -> dict | None:
    """
    Get the previous trading day's OHLCV quote for a ticker.
    Returns {ticker, open, high, low, close, volume, vwap, timestamp, source}.
    """
    return get_previous_close(ticker.upper())


@mcp.tool()
def get_quotes(tickers: list[str]) -> dict[str, dict]:
    """
    Get previous day's OHLCV quotes for multiple tickers at once (cached).
    Returns {ticker: {open, high, low, close, volume, vwap, ...}}
    """
    return get_bulk_previous_close([t.upper() for t in tickers])


@mcp.tool()
def get_price_history(
    ticker: str,
    days: int = 200,
    timespan: str = "day",
) -> list[dict]:
    """
    Get historical OHLCV bars for a ticker.

    Args:
        ticker:   e.g. 'NVDA'
        days:     how many calendar days back to fetch (default 200)
        timespan: 'minute' | 'hour' | 'day' | 'week' | 'month'

    Returns list of {timestamp, open, high, low, close, volume, vwap}
    sorted oldest to newest — ready for technical analysis.
    """
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")
    return get_bars(ticker.upper(), 1, timespan, from_date, to_date)


@mcp.tool()
def get_ticker_info(ticker: str) -> dict | None:
    """
    Get fundamental details for a ticker.
    Returns {ticker, name, market_cap, primary_exchange, type, description}.
    """
    return get_ticker_details(ticker.upper())


# ============================================================
# TECHNICAL ANALYSIS TOOLS (Component C6)
# ============================================================

@mcp.tool()
def analyze_ticker(ticker: str, days: int = 300) -> dict:
    """
    Full technical analysis for a ticker.

    Computes: MA20/50/200, EMA20, RSI(14), MACD(12,26,9),
    Bollinger Bands(20,2), ATR(14), relative volume,
    support/resistance levels, trend direction,
    signal (BUY/SELL/NEUTRAL), and strength score (0-100).

    Args:
        ticker: e.g. 'NVDA'
        days:   calendar days of history to analyze (default 300 for MA200)

    Returns complete technical profile with plain-English summary.
    """
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")
    bars = get_bars(ticker.upper(), 1, "day", from_date, to_date)
    return get_technical_profile(ticker.upper(), bars)


# ============================================================
# OPTIONS FLOW TOOLS (Component C4 — Unusual Whales)
# ============================================================

@mcp.tool()
def get_market_overview() -> dict:
    """
    Full market-wide options flow overview.
    Returns market tide (net call/put premium), total options volume,
    sector ETF flow, and upcoming economic calendar events.
    Tells you the macro direction of institutional money right now.
    """
    from app.options_flow.unusual_whales import (
        get_market_tide, get_total_options_volume,
        get_sector_etfs, get_economic_calendar,
    )
    from app.options_flow.signals import score_market_tide

    tide = get_market_tide()
    tide_score = score_market_tide(tide)
    total_vol = get_total_options_volume()
    sectors = get_sector_etfs()
    econ = get_economic_calendar()

    # Top bullish/bearish sectors
    bullish_sectors = sorted(
        [s for s in sectors if float(s.get("bullish_premium") or 0) > float(s.get("bearish_premium") or 0)],
        key=lambda x: float(x.get("bullish_premium") or 0), reverse=True
    )[:3]
    bearish_sectors = sorted(
        [s for s in sectors if float(s.get("bearish_premium") or 0) > float(s.get("bullish_premium") or 0)],
        key=lambda x: float(x.get("bearish_premium") or 0), reverse=True
    )[:3]

    # Upcoming events in next 7 days
    upcoming_events = econ[:10]

    return {
        "market_tide": tide_score,
        "total_options_volume": total_vol,
        "bullish_sectors": [{"ticker": s["ticker"], "name": s.get("full_name"), "bullish_premium": s.get("bullish_premium")} for s in bullish_sectors],
        "bearish_sectors": [{"ticker": s["ticker"], "name": s.get("full_name"), "bearish_premium": s.get("bearish_premium")} for s in bearish_sectors],
        "upcoming_economic_events": upcoming_events,
    }


@mcp.tool()
def get_options_flow(ticker: str | None = None, min_premium: float = 500000, sweeps_only: bool = False) -> list[dict]:
    """
    Recent options flow alerts (institutional sweep activity).
    Each alert shows: ticker, type (call/put), total_premium, has_sweep,
    volume, open_interest, volume_oi_ratio, strike, expiry, sector.

    Args:
        ticker:       filter to specific ticker (None = all market)
        min_premium:  minimum total premium in $ (default $500K)
        sweeps_only:  if True, only sweeps (multi-exchange aggressive buys)
    """
    from app.options_flow.unusual_whales import get_flow_alerts
    return get_flow_alerts(ticker=ticker, min_premium=min_premium, sweeps_only=sweeps_only)


@mcp.tool()
def get_dark_pool(ticker: str | None = None, min_premium: float = 0) -> list[dict]:
    """
    Recent dark pool (off-exchange institutional block) trades.
    Each trade shows: ticker, premium, size, price, executed_at,
    nbbo_ask, nbbo_bid (compare price to these to infer direction).

    Args:
        ticker:       specific ticker (None = all recent market dark pool)
        min_premium:  minimum trade premium in $ (e.g. 2000000 = $2M+)
    """
    from app.options_flow.unusual_whales import get_dark_pool_ticker, get_dark_pool_recent
    if ticker:
        return get_dark_pool_ticker(ticker.upper())
    return get_dark_pool_recent(min_premium=min_premium)


@mcp.tool()
def get_gex(ticker: str) -> dict:
    """
    Dealer Gamma Exposure (GEX) analysis for a ticker.
    Returns: net_gamma (pos=stabilizing, neg=volatile), gamma_wall (key price level),
    GEX by strike (shows where dealers must hedge), and GEX by expiry.

    The gamma wall is the strongest gravitational price level.
    Negative GEX below current price = downside acceleration zone.
    """
    from app.options_flow.unusual_whales import get_greek_exposure, get_gex_by_strike, get_gex_by_expiry
    from app.options_flow.signals import score_gex
    from app.market_data.polygon_client import get_previous_close

    gex = get_greek_exposure(ticker)
    by_strike = get_gex_by_strike(ticker)
    by_expiry = get_gex_by_expiry(ticker)
    quote = get_previous_close(ticker.upper())
    current_price = quote["close"] if quote else 0

    scored = score_gex(gex, by_strike, current_price)

    # Top 5 strikes with most GEX (gamma walls)
    top_strikes = sorted(
        [s for s in by_strike if s.get("call_gex") or s.get("put_gex")],
        key=lambda x: abs(float(x.get("call_gex") or 0) + float(x.get("put_gex") or 0)),
        reverse=True
    )[:5]

    return {
        "ticker": ticker.upper(),
        "current_price": current_price,
        "gex_score": scored,
        "top_gex_strikes": top_strikes,
        "gex_by_expiry": by_expiry[:10],
    }


@mcp.tool()
def get_ticker_signal(ticker: str) -> dict:
    """
    Complete options flow signal for a ticker — the primary input to the Strategy Engine.

    Combines: options flow sweeps, dark pool, GEX, market tide, earnings risk.
    Returns: direction (BULLISH/BEARISH/NEUTRAL), confidence (0-100),
    signal (STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL/BLOCKED),
    and plain-English summary of all signals.

    This is the tool to call before get_strategy_recommendation().
    """
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package

    pkg = get_signal_package(ticker.upper())
    return score_signal_package(pkg)


@mcp.tool()
def get_earnings_calendar() -> dict:
    """
    Today's earnings reports (pre-market and after-hours).
    Critical for Rule 2: avoid new options positions within 7 days of earnings.
    Returns lists of companies reporting today with expected move percentages.
    """
    from app.options_flow.unusual_whales import get_earnings_afterhours, get_earnings_premarket
    return {
        "premarket": get_earnings_premarket(),
        "afterhours": get_earnings_afterhours(),
    }


@mcp.tool()
def get_news(ticker: str | None = None) -> list[dict]:
    """
    Recent market news headlines with sentiment scoring.
    Each item: headline, source, created_at, tickers, is_major, sentiment.

    Args:
        ticker: filter to ticker-specific news (None = all major headlines)
    """
    from app.options_flow.unusual_whales import get_news_headlines
    return get_news_headlines(ticker=ticker)


@mcp.tool()
def get_congress_trades(ticker: str | None = None) -> list[dict]:
    """
    Recent congressional stock trades (senators + representatives).
    Each trade: name, ticker, txn_type (buy/sell), amounts, transaction_date.
    Congressional buys have historically preceded major price moves.

    Args:
        ticker: filter to specific ticker (None = all recent congress trades)
    """
    from app.options_flow.unusual_whales import get_congress_trades as _get_congress
    return _get_congress(ticker=ticker)


# ============================================================
# STRATEGY ENGINE (Component C7) — THE CORE TRADE RECOMMENDER
# ============================================================

@mcp.tool()
def get_strategy_recommendation(
    ticker: str,
    budget: float = 2000.0,
    max_loss: float | None = None,
    profit_target: float | None = None,
    min_dte: int = 4,
    max_dte: int = 365,
) -> dict:
    """
    THE PRIMARY TOOL — scans ALL available expiries (4 DTE → 365 DTE) and returns
    the optimal trade recommendation that best fits your constraints.

    Example output:
        "NVDA DEBIT_PUT_SPREAD — BEARISH
         BUY $205P / SELL $195P — Jun 27 (10 DTE)
         8 contracts @ $1.85 debit = $1,480 total
         Target: +$2,368 (+160%) | Stop: -$592 (-40%)
         R/R: 4.0 | Confidence: 68/100
         Alternatives: Jul 10 (23 DTE), Jul 18 (31 DTE)"

    Args:
        ticker:         stock ticker e.g. 'NVDA', 'SPY', 'AMD'
        budget:         max capital to invest in dollars (default $2,000)
        max_loss:       max acceptable loss in $ (default: budget × 40%)
        profit_target:  minimum profit you want in $ (filters out expiries that can't hit this)
        min_dte:        minimum days to expiry (default: 4 = this week)
        max_dte:        maximum days to expiry (default: 365, set 911+ for LEAPS)

    Returns best recommendation + 2 alternatives scored by R/R × confidence.

    ⚠️ Educational analysis only. Not financial advice. Always apply
    Rule 3 regime check and Rule 4 pre-trade checklist before executing.
    """
    from datetime import datetime, timedelta
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package
    from app.strategy.engine import build_recommendation

    ticker = ticker.upper()

    # Step 1: Technical Analysis
    from_date = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
    to_date   = datetime.now().strftime("%Y-%m-%d")
    bars      = get_bars(ticker, 1, "day", from_date, to_date)
    ta_profile = get_technical_profile(ticker, bars)

    # Step 2: Options Flow Signal
    pkg         = get_signal_package(ticker)
    flow_signal = score_signal_package(pkg)

    # Step 3: Scan all expiries and find best trade
    return build_recommendation(
        ticker        = ticker,
        ta_profile    = ta_profile,
        flow_signal   = flow_signal,
        budget        = budget,
        max_loss      = max_loss,
        profit_target = profit_target,
        min_dte       = min_dte,
        max_dte       = max_dte,
    )



# ============================================================
# WATCHLIST TOOLS (Component C5 — Discovery)
# ============================================================

@mcp.tool()
def get_watchlist() -> list[dict]:
    """
    Get all tickers in your watchlist.
    Returns [{ticker, notes, sector, added_at}]
    """
    from app.db.queries.watchlist import get_watchlist as _get_wl
    user_id = get_current_user_id()
    return _get_wl(user_id)


@mcp.tool()
def add_to_watchlist(ticker: str, notes: str = "", sector: str = "") -> dict:
    """
    Add a ticker to your watchlist.

    Args:
        ticker: stock ticker e.g. 'NVDA'
        notes:  optional notes e.g. 'watching for breakout above 220'
        sector: optional sector e.g. 'Semiconductors'
    """
    from app.db.queries.watchlist import add_to_watchlist as _add
    user_id = get_current_user_id()
    added   = _add(user_id, ticker.upper(), notes, sector)
    return {"ticker": ticker.upper(), "added": added,
            "message": f"{'Added' if added else 'Already in'} watchlist"}


@mcp.tool()
def remove_from_watchlist(ticker: str) -> dict:
    """Remove a ticker from your watchlist."""
    from app.db.queries.watchlist import remove_from_watchlist as _remove
    user_id = get_current_user_id()
    removed = _remove(user_id, ticker.upper())
    return {"ticker": ticker.upper(), "removed": removed}


@mcp.tool()
def get_scan_universe(
    extra_tickers: list[str] | None = None,
    min_market_cap: float = 0,
    sectors: list[str] | None = None,
    min_price: float = 0,
) -> list[str]:
    """
    Get the full list of tickers to scan today.

    Returns watchlist + current positions + filtered universe.
    Use this before running bulk analysis across multiple tickers.

    Args:
        extra_tickers:   additional tickers to always include
        min_market_cap:  filter by minimum market cap (e.g. 10000000000 = $10B)
        sectors:         filter by sector (e.g. ['Technology', 'Semiconductors'])
        min_price:       filter by minimum stock price
    """
    from app.scanner.universe import get_scan_universe_mcp
    return get_scan_universe_mcp(
        extra_tickers  = extra_tickers,
        min_market_cap = min_market_cap,
        sectors        = sectors,
        min_price      = min_price,
    )


@mcp.tool()
def scan_tickers(
    tickers: list[str] | None = None,
    budget: float = 2000.0,
    max_loss: float | None = None,
    top_n: int = 5,
) -> list[dict]:
    """
    Run full analysis across multiple tickers and return the top setups.

    If tickers is None, uses your watchlist + positions automatically.
    Runs TA + options flow signal on each ticker and ranks by confidence × R/R.

    Args:
        tickers:  list of tickers to scan (None = use watchlist + positions)
        budget:   budget per trade (default $2,000)
        max_loss: max loss per trade (default budget × 40%)
        top_n:    number of top setups to return (default 5)

    Returns ranked list of trade setups ready for get_strategy_recommendation.
    """
    from datetime import datetime, timedelta
    from app.scanner.universe import get_scan_universe
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package

    user_id = get_current_user_id()

    # Get universe
    if tickers:
        universe = [t.upper() for t in tickers]
    else:
        universe = get_scan_universe(user_id=user_id, max_tickers=30)

    results = []
    for ticker in universe:
        try:
            # Quick TA
            from_date  = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
            to_date    = datetime.now().strftime("%Y-%m-%d")
            bars       = get_bars(ticker, 1, "day", from_date, to_date)
            ta         = get_technical_profile(ticker, bars)

            # Flow signal
            pkg  = get_signal_package(ticker)
            flow = score_signal_package(pkg)

            # Quick score
            ta_score   = ta.get("strength_score", 50)
            flow_conf  = flow.get("confidence", 50)
            combined   = round((ta_score * 0.4 + flow_conf * 0.6), 1)
            direction  = flow.get("direction", "NEUTRAL")
            blocked    = flow.get("trade_blocked", False)

            if not blocked and combined >= 45:
                results.append({
                    "ticker":      ticker,
                    "direction":   direction,
                    "confidence":  combined,
                    "ta_signal":   ta.get("signal"),
                    "ta_score":    ta_score,
                    "flow_signal": flow.get("signal"),
                    "flow_conf":   flow_conf,
                    "summary":     ta.get("summary"),
                    "flow_summary":flow.get("summary"),
                    "earnings_risk": flow.get("earnings_risk", {}).get("risk"),
                    "days_to_earnings": flow.get("earnings_risk", {}).get("days_to_earnings"),
                })
        except Exception as e:
            print(f"[Scanner] {ticker} failed: {e}")
            continue

    # Rank by confidence
    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results[:top_n]

@mcp.tool()
def scan_watchlist(
    budget: float = 2000.0,
    max_loss: float | None = None,
    top_n: int = 5,
) -> dict:
    """
    THE DAILY ANALYSIS TOOL — scans your entire Webull watchlist (127 stocks)
    using the Two-Tier Convergence Scanner.

    Tier 1 (30s): Scores ALL watchlist tickers on:
      - Price momentum (±2%+ today via yfinance)
      - Options flow (unusual sweeps via UW)
      - Dark pool (institutional prints via UW)
      Selects top 5 where ≥2 signals converge in the same direction.

    Tier 2 (60-90s): Deep LLM analysis on top 5:
      - Stock price: Webull → yfinance → Polygon (most current)
      - Options prices/IV/volume: UW exclusively
      - Greeks: BSM with UW IV + live spot
      - LLM decides strategy + strikes from full data package
      - Python executes exact math with real UW prices

    Returns 2-3 complete trade setups with specific strikes, entry,
    target, stop, and Webull order instructions. Total ~2 minutes.
    """
    from app.scanner.quick_scan import run_full_scan
    return run_full_scan(budget=budget, max_loss=max_loss, top_quick=top_n)

"""
W5: Sell Signals + Portfolio P&L (Phase 1 Completion).

Two-tier exit analysis:
    Tier 1 (instant): Rule-based signals — catches obvious exits
        - Rule 1A: 80% of max profit reached → TAKE PROFIT
        - Rule 1B: 40% loss of cost → STOP LOSS
        - Technical reversal: TA signal flips against position
        - Earnings risk: earnings within 7 days
        - Time decay: options < 7 DTE

    Tier 2 (LLM-enhanced): For each actionable signal, LLM evaluates with context:
        - Position details + P&L
        - TA signal + momentum
        - UW options flow (is smart money still bullish?)
        - Sector performance (are peers moving same way?)
        - Earnings proximity + historical reaction sizes
        - RAG context (once C8 is built): news, filings, macro
        LLM returns: nuanced recommendation (partial exit? roll? hold?),
                     reasoning, and confidence level

This matters because:
    Rule-based: "SNDK +287% → SELL"
    LLM-enhanced: "Take 60% profit on SNDK — storage sector momentum
                   persists (WDC +171% confirms), keep 40% with trailing
                   stop. Earnings in 18 days adds risk — plan full exit by then."
"""
from datetime import datetime, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# Sell Signal Rules
# ─────────────────────────────────────────────────────────────────────────────

TAKE_PROFIT_PCT = 0.80    # Rule 1A: exit at 80% max profit (options) or 20% stock gain
STOP_LOSS_PCT   = -0.40   # Rule 1B: exit at 40% loss
EARNINGS_BUFFER = 7       # days — exit before earnings to avoid IV crush
MIN_DTE         = 7       # exit options with < 7 DTE remaining
WEAK_VS_SPY     = -0.05   # flag if stock underperforms SPY by 5%+ today


def _get_ta_signal(ticker: str) -> str:
    """Get quick TA signal for a ticker. Returns BUY / SELL / NEUTRAL."""
    try:
        from datetime import datetime, timedelta
        from app.market_data.polygon_client import get_bars
        from app.technical_analysis.engine import get_technical_profile

        from_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        to_date   = datetime.now().strftime("%Y-%m-%d")
        bars      = get_bars(ticker, 1, "day", from_date, to_date)
        ta        = get_technical_profile(ticker, bars)
        return ta.get("signal", "NEUTRAL")
    except Exception:
        return "NEUTRAL"


def _get_earnings_days(ticker: str) -> int | None:
    """Return days to next earnings or None if unknown."""
    try:
        from app.options_flow.unusual_whales import get_ticker_earnings_history
        history = get_ticker_earnings_history(ticker)
        if not history:
            return None
        # Find the next upcoming earnings
        today = datetime.now().date()
        for e in history:
            date_str = e.get("report_date") or e.get("earnings_date") or ""
            if not date_str:
                continue
            try:
                earn_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
                if earn_date >= today:
                    return (earn_date - today).days
            except Exception:
                pass
        return None
    except Exception:
        return None


def _parse_option_dte(symbol: str) -> int | None:
    """Extract DTE from option symbol like NVDA260710C00205000."""
    try:
        import re
        m = re.search(r"(\d{6})[CP]", symbol)
        if m:
            exp = datetime.strptime("20" + m.group(1), "%Y%m%d")
            return (exp - datetime.now()).days
    except Exception:
        pass
    return None


def evaluate_sell_signals(positions: list[dict]) -> list[dict]:
    """
    Evaluate every open position for exit signals.

    Args:
        positions: list from WebullConnector.get_positions()

    Returns:
        list of {symbol, action, urgency, reason, pnl_pct, signals}
        action: SELL / WATCH / HOLD
        urgency: HIGH / MEDIUM / LOW
    """
    results = []

    for pos in positions:
        symbol   = pos.get("symbol", "")
        qty      = float(pos.get("qty", 0))
        cost     = float(pos.get("total_cost", 0))
        value    = float(pos.get("market_value", 0))
        pnl      = float(pos.get("unrealized_profit_loss", 0))
        pnl_rate = float(pos.get("unrealized_profit_loss_rate", 0))  # decimal e.g. -0.17
        inst_type = pos.get("instrument_type", "STOCK")

        if qty == 0 or cost == 0:
            continue

        signals = []
        action  = "HOLD"
        urgency = "LOW"

        # ── Rule 1B: Stop Loss (-40% loss) ───────────────────────────────────
        if pnl_rate <= STOP_LOSS_PCT:
            signals.append(f"STOP LOSS: {pnl_rate*100:.1f}% loss (rule: exit at -40%)")
            action  = "SELL"
            urgency = "HIGH"

        # ── Rule 1A: Take Profit (stock +20%, or option +80%) ────────────────
        profit_threshold = TAKE_PROFIT_PCT if inst_type == "OPTION" else 0.20
        if pnl_rate >= profit_threshold:
            signals.append(f"TAKE PROFIT: +{pnl_rate*100:.1f}% gain (rule: exit at +{profit_threshold*100:.0f}%)")
            if action != "SELL":
                action  = "SELL"
                urgency = "HIGH"

        # ── Options-specific: DTE check ───────────────────────────────────────
        if inst_type == "OPTION":
            dte = _parse_option_dte(symbol)
            if dte is not None and dte <= MIN_DTE:
                signals.append(f"DTE WARNING: {dte} days remaining — theta decay accelerating")
                if action == "HOLD":
                    action  = "SELL"
                    urgency = "HIGH"

        # ── Earnings risk (both stocks and options) ───────────────────────────
        earn_days = _get_earnings_days(symbol.split()[0])  # handle "NVDA 260710C" format
        if earn_days is not None and earn_days <= EARNINGS_BUFFER:
            signals.append(f"EARNINGS IN {earn_days}d — exit before IV crush/gap risk")
            if action == "HOLD":
                action  = "WATCH"
                urgency = "MEDIUM"
            elif action == "SELL" and urgency == "LOW":
                urgency = "MEDIUM"

        # ── TA reversal signal ────────────────────────────────────────────────
        ticker = symbol.split()[0]
        if len(ticker) <= 6:  # skip complex option symbols
            ta_signal = _get_ta_signal(ticker)
            # If we're holding a LONG position but TA says SELL
            if ta_signal == "SELL" and pnl_rate > -0.10:  # only flag if not already stopped
                signals.append(f"TA REVERSAL: {ticker} showing SELL signal")
                if action == "HOLD":
                    action  = "WATCH"
                    urgency = "MEDIUM"

        # ── Significant loss watch ────────────────────────────────────────────
        if -0.40 < pnl_rate <= -0.25 and action == "HOLD":
            signals.append(f"LOSS WATCH: {pnl_rate*100:.1f}% — approaching stop loss threshold")
            action  = "WATCH"
            urgency = "MEDIUM"

        results.append({
            "symbol":          symbol,
            "instrument_type": inst_type,
            "action":          action,
            "urgency":         urgency,
            "pnl":             round(pnl, 2),
            "pnl_pct":         round(pnl_rate * 100, 2),
            "cost":            round(cost, 2),
            "market_value":    round(value, 2),
            "qty":             qty,
            "signals":         signals,
            "earnings_days":   earn_days,
        })

    # Sort: HIGH urgency first, then by worst P&L
    results.sort(key=lambda x: (
        {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(x["urgency"], 3),
        x["pnl_pct"]
    ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio P&L
# ─────────────────────────────────────────────────────────────────────────────

def get_portfolio_pnl_summary(
    positions: list[dict],
    balances: dict | None = None,
) -> dict:
    """
    Calculate full portfolio P&L snapshot.

    Args:
        positions: from WebullConnector.get_positions()
        balances:  from WebullConnector.get_balances() (optional, for buying power)

    Returns:
        Complete portfolio summary with per-position breakdown
    """
    if not positions:
        return {"error": "No positions found"}

    total_cost   = 0.0
    total_value  = 0.0
    total_pnl    = 0.0
    stock_value  = 0.0
    option_value = 0.0

    breakdown = []
    for pos in positions:
        cost     = float(pos.get("total_cost", 0))
        value    = float(pos.get("market_value", 0))
        pnl      = float(pos.get("unrealized_profit_loss", 0))
        pnl_rate = float(pos.get("unrealized_profit_loss_rate", 0))
        inst_type = pos.get("instrument_type", "STOCK")

        total_cost  += cost
        total_value += value
        total_pnl   += pnl

        if inst_type == "OPTION":
            option_value += value
        else:
            stock_value += value

        breakdown.append({
            "symbol":       pos.get("symbol"),
            "type":         inst_type,
            "qty":          float(pos.get("qty", 0)),
            "cost":         round(cost, 2),
            "value":        round(value, 2),
            "pnl":          round(pnl, 2),
            "pnl_pct":      round(pnl_rate * 100, 2),
            "weight":       round(value / max(total_value, 1) * 100, 1),
            "unit_cost":    float(pos.get("unit_cost", 0)),
            "last_price":   float(pos.get("last_price", 0)),
        })

    # Sort by P&L
    breakdown.sort(key=lambda x: x["pnl"], reverse=True)

    # Recalculate weights with final total
    for b in breakdown:
        b["weight"] = round(b["value"] / max(total_value, 1) * 100, 1)

    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    # Best/worst
    winners = [b for b in breakdown if b["pnl"] > 0]
    losers  = [b for b in breakdown if b["pnl"] < 0]

    # Buying power from balances
    buying_power  = None
    cash          = None
    account_value = None
    if balances:
        buying_power  = balances.get("buying_power") or balances.get("cash_available_for_trade")
        cash          = balances.get("cash_balance") or balances.get("net_cash")
        account_value = balances.get("total_account_value") or balances.get("net_liquidation")

    return {
        # Totals
        "total_cost":       round(total_cost, 2),
        "total_value":      round(total_value, 2),
        "total_pnl":        round(total_pnl, 2),
        "total_pnl_pct":    round(total_pnl_pct, 2),
        "position_count":   len(positions),
        "stock_value":      round(stock_value, 2),
        "option_value":     round(option_value, 2),

        # Account
        "buying_power":     buying_power,
        "cash":             cash,
        "account_value":    account_value,

        # Performance
        "winners":          len(winners),
        "losers":           len(losers),
        "win_rate":         round(len(winners) / max(len(positions), 1) * 100, 1),
        "best_performer":   breakdown[0]["symbol"] if breakdown else None,
        "worst_performer":  breakdown[-1]["symbol"] if breakdown else None,
        "biggest_gain":     round(max((b["pnl"] for b in breakdown), default=0), 2),
        "biggest_loss":     round(min((b["pnl"] for b in breakdown), default=0), 2),

        # Detail
        "positions": breakdown,

        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M ET"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM-Enhanced Sell Analysis
# ─────────────────────────────────────────────────────────────────────────────

def _build_position_context(signal: dict, all_positions: list[dict]) -> str:
    """
    Build rich context string for LLM analysis of one position.
    Includes sector peers, flow data, and TA signal.
    """
    sym      = signal["symbol"].split()[0]
    pnl_pct  = signal["pnl_pct"]
    cost     = signal["cost"]
    value    = signal["market_value"]
    inst     = signal["instrument_type"]

    lines = [
        f"POSITION: {sym} ({inst})",
        f"  Entry cost: ${cost:,.2f} | Current value: ${value:,.2f}",
        f"  Unrealized P&L: ${signal['pnl']:,.2f} ({pnl_pct:+.1f}%)",
        f"  Qty: {signal['qty']}",
        f"  Rule signals fired: {', '.join(signal['signals']) or 'none'}",
    ]

    if signal.get("earnings_days") is not None:
        lines.append(f"  Earnings in {signal['earnings_days']} days")

    # Add TA signal
    try:
        ta = _get_ta_signal(sym)
        lines.append(f"  TA signal: {ta}")
    except Exception:
        pass

    # Add UW flow summary
    try:
        from app.options_flow.unusual_whales import get_flow_alerts, get_dark_pool_ticker
        alerts = get_flow_alerts(ticker=sym, limit=5)
        if alerts:
            bull = sum(1 for a in alerts if a.get("sentiment") in ("BULLISH","CALL"))
            bear = sum(1 for a in alerts if a.get("sentiment") in ("BEARISH","PUT"))
            lines.append(f"  Options flow (last 5): {bull} bullish / {bear} bearish")

        dp = get_dark_pool_ticker(sym, limit=5)
        if dp:
            dp_buy  = sum(1 for d in dp if d.get("side") in ("BUY","A"))
            dp_sell = sum(1 for d in dp if d.get("side") in ("SELL","B"))
            lines.append(f"  Dark pool (last 5): {dp_buy} buy / {dp_sell} sell")
    except Exception:
        pass

    # Add sector peers from same portfolio
    peers = [
        p for p in all_positions
        if p["symbol"] != signal["symbol"]
        and float(p.get("unrealized_profit_loss_rate", 0)) != 0
    ]
    if peers:
        peer_summary = ", ".join(
            f"{p['symbol']} {float(p['unrealized_profit_loss_rate'])*100:+.0f}%"
            for p in peers[:4]
        )
        lines.append(f"  Portfolio peers: {peer_summary}")

    return "\n".join(lines)


def evaluate_sell_signals_with_llm(
    positions: list[dict],
    user_id: str | None = None,
) -> list[dict]:
    """
    LLM-enhanced sell signal evaluation.

    Tier 1: Rule-based signals fire first (instant, always reliable)
    Tier 2: LLM evaluates each actionable signal with full context:
        - Position details + P&L
        - TA signal
        - UW options flow
        - Sector peer performance
        - Earnings proximity
        Returns: nuanced recommendation, reasoning, partial exit %, confidence

    Args:
        positions: from WebullConnector.get_positions()
        user_id:   for broker context

    Returns:
        Enhanced signals with LLM reasoning added to each actionable one
    """
    # Tier 1: Rule-based
    signals    = evaluate_sell_signals(positions)
    actionable = [s for s in signals if s["action"] in ("SELL", "WATCH")]

    if not actionable:
        return signals

    # Tier 2: LLM analysis for each actionable signal
    try:
        from app.llm.service import LLMService
        llm = LLMService()

        for signal in actionable:
            try:
                context = _build_position_context(signal, positions)

                prompt = f"""You are an expert options and stock trader analyzing exit decisions.

{context}

TASK: Provide a nuanced sell recommendation. Consider:
1. Is this a full exit or partial profit-taking?
2. Does the momentum/sector suggest the trend continues?
3. Any upcoming catalysts (earnings, Fed meetings) that change the thesis?
4. For large gains: should the trader roll up strikes or take profit?
5. For large losses: is this a fundamentals change or temporary weakness?

Respond in this exact format:
ACTION: [FULL_EXIT / PARTIAL_EXIT / HOLD / ROLL]
EXIT_PCT: [0-100% of position to exit]
CONFIDENCE: [HIGH / MEDIUM / LOW]
SUMMARY: [One sentence recommendation]
REASONING: [2-3 sentences explaining the nuanced view]
RISK: [One sentence about the main risk of this recommendation]"""

                response = llm.complete(prompt, max_tokens=300)

                # Parse LLM response
                llm_action    = "HOLD"
                llm_exit_pct  = 100
                llm_conf      = "MEDIUM"
                llm_summary   = ""
                llm_reasoning = ""
                llm_risk      = ""

                for line in response.splitlines():
                    line = line.strip()
                    if line.startswith("ACTION:"):
                        llm_action = line.split(":",1)[1].strip()
                    elif line.startswith("EXIT_PCT:"):
                        try:
                            llm_exit_pct = int(line.split(":",1)[1].strip().replace("%",""))
                        except Exception:
                            pass
                    elif line.startswith("CONFIDENCE:"):
                        llm_conf = line.split(":",1)[1].strip()
                    elif line.startswith("SUMMARY:"):
                        llm_summary = line.split(":",1)[1].strip()
                    elif line.startswith("REASONING:"):
                        llm_reasoning = line.split(":",1)[1].strip()
                    elif line.startswith("RISK:"):
                        llm_risk = line.split(":",1)[1].strip()

                signal["llm"] = {
                    "action":     llm_action,
                    "exit_pct":   llm_exit_pct,
                    "confidence": llm_conf,
                    "summary":    llm_summary,
                    "reasoning":  llm_reasoning,
                    "risk":       llm_risk,
                }

            except Exception as e:
                signal["llm"] = {"error": str(e)}

    except Exception as e:
        # LLM unavailable — return rule-based only
        for s in actionable:
            s["llm"] = {"error": f"LLM unavailable: {e}"}

    return signals


def format_sell_report(signals: list[dict], pnl: dict) -> str:
    """
    Format sell signals + P&L into a clean Claude Desktop response.
    """
    lines = []

    # Header
    lines.append("## Portfolio P&L Summary")
    lines.append(f"**Total Value:** ${pnl['total_value']:,.2f} | "
                 f"**P&L:** ${pnl['total_pnl']:,.2f} ({pnl['total_pnl_pct']:+.2f}%) | "
                 f"**Win Rate:** {pnl['win_rate']}% ({pnl['winners']}W/{pnl['losers']}L)")
    if pnl.get("buying_power"):
        lines.append(f"**Buying Power:** ${float(pnl['buying_power']):,.2f}")
    lines.append("")

    # Actionable signals
    actionable = [s for s in signals if s["action"] in ("SELL","WATCH")]
    if actionable:
        lines.append("## Exit Recommendations")
        for s in actionable:
            emoji = "🔴" if s["action"] == "SELL" else "🟡"
            lines.append(f"\n{emoji} **{s['symbol']}** — {s['action']} ({s['urgency']} urgency) | P&L: {s['pnl_pct']:+.1f}%")
            for sig in s["signals"]:
                lines.append(f"  - {sig}")
            if s.get("llm") and not s["llm"].get("error"):
                llm = s["llm"]
                lines.append(f"  💬 **LLM:** {llm.get('summary','')}")
                if llm.get("exit_pct", 100) < 100:
                    lines.append(f"  📊 **Exit:** {llm['exit_pct']}% of position | Confidence: {llm.get('confidence','')}")
                if llm.get("reasoning"):
                    lines.append(f"  📝 {llm['reasoning']}")
                if llm.get("risk"):
                    lines.append(f"  ⚠️ Risk: {llm['risk']}")
    else:
        lines.append("## Exit Recommendations\n✅ No urgent exit signals at this time.")

    return "\n".join(lines)

@mcp.tool()
def get_sell_signals(use_llm: bool = True) -> dict:
    """
    Analyze all open positions for exit signals.
    Tier 1: Rule-based — stop loss, take profit, DTE, earnings, TA reversal.
    Tier 2: LLM batch — nuanced recommendation for ALL positions including HOLDs.
    Checks past recommendations — flags if signal was previously ignored.
    Returns: actionable exits with LLM reasoning + hold assessments.
    """
    from app.broker.webull_connector import WebullConnector
    from app.broker.sell_signals import (
        evaluate_sell_signals,
        evaluate_sell_signals_with_llm,
        format_sell_report,
        get_portfolio_pnl_summary,
    )
    from app.utils.current_user import get_current_user_id

    user_id = get_current_user_id()
    pos     = WebullConnector(user_id).get_positions()
    pnl     = get_portfolio_pnl_summary(pos, None)
    fn      = evaluate_sell_signals_with_llm if use_llm else evaluate_sell_signals
    signals = fn(pos, user_id=user_id) if use_llm else fn(pos)
    return {"report": format_sell_report(signals, pnl), "signals": signals, "pnl": pnl}


@mcp.tool()
def get_portfolio_pnl() -> dict:
    """
    Full portfolio P&L snapshot.
    Returns: total value, cost, unrealized P&L, win rate,
    per-position breakdown sorted by P&L, best/worst performers.
    """
    from app.broker.webull_connector import WebullConnector
    from app.broker.sell_signals import get_portfolio_pnl_summary
    from app.utils.current_user import get_current_user_id

    user_id = get_current_user_id()
    wb      = WebullConnector(user_id)
    pos     = wb.get_positions()
    try:    bal = wb.get_balances()
    except: bal = None
    return get_portfolio_pnl_summary(pos, bal)


if __name__ == "__main__":
    mcp.run()