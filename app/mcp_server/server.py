"""
MCP Server — Trading Intelligence Platform.

Phase 1:  Webull broker + market data + sell signals + portfolio P&L
Phase 2:  Options flow + technical analysis + strategy engine + scanner + watchlist
Phase 3+: RAG pipeline, position monitor, notifications, prediction tracker
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastmcp import FastMCP
from sqlalchemy import text

from app.broker.base import BrokerNotConnectedError
from app.broker.webull_connector import WebullConnector
from app.db.session import get_session
from app.market_data.polygon_client import (
    get_bars, get_bulk_previous_close,
    get_previous_close, get_ticker_details,
)
from app.technical_analysis.engine import get_technical_profile
from app.utils.current_user import get_current_user_id

mcp = FastMCP("Personal Trading Intelligence Platform")


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def ping() -> str:
    """Health check — confirms MCP server is running and DB is connected."""
    with get_session() as session:
        row = session.execute(
            text("SELECT email, display_name FROM users LIMIT 1")
        ).fetchone()
        if row:
            return f"pong — DB connected. User: {row.display_name} ({row.email})"
        return "pong — DB connected but no users found."


# ─────────────────────────────────────────────────────────────────────────────
# WEBULL BROKER TOOLS (W2)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_positions() -> list[dict]:
    """
    Fetch current live positions from Webull (stocks and options).
    Returns list of {symbol, instrument_type, qty, unit_cost, last_price,
    market_value, total_cost, unrealized_profit_loss, unrealized_profit_loss_rate}.
    """
    user_id = get_current_user_id()
    try:
        return WebullConnector(user_id).get_positions()
    except BrokerNotConnectedError:
        return [{"error": "Webull not connected."}]


@mcp.tool()
def get_balances() -> dict:
    """
    Fetch current account balance, cash, and buying power from Webull.
    Returns {total_asset_currency, total_market_value, total_cash_balance, ...}.
    """
    user_id = get_current_user_id()
    try:
        return WebullConnector(user_id).get_balance()
    except BrokerNotConnectedError:
        return {"error": "Webull not connected."}


@mcp.tool()
def get_orders() -> list[dict]:
    """
    Fetch today's orders from Webull.
    Returns list of orders with {symbol, side, order_status, filled_price, qty}.
    """
    user_id = get_current_user_id()
    try:
        return WebullConnector(user_id).get_orders()
    except BrokerNotConnectedError:
        return [{"error": "Webull not connected."}]

@mcp.tool()
def get_active_bets() -> dict:
    """
    All current positions with full trade context — the trader's dashboard.

    For each position shows:
    - How much invested (cost basis) vs current value
    - P&L amount and %
    - Target exit: price, %, potential gain $
    - Stop loss: price, %, potential loss $
    - Distance remaining to target and stop
    - Status: TARGET_HIT / NEAR_TARGET / ON_TRACK / NEAR_STOP / STOP_HIT
    - Source: from our recommendation engine or manually opened
    - How many times we already recommended selling (ignored signals)

    Sorted by urgency: stop hits first, then near-stop, then target hits.
    """
    from app.broker.active_bets import get_active_bets as _get, format_bets_report
    user_id  = get_current_user_id()
    pos      = WebullConnector(user_id).get_positions()
    bets     = _get(pos, user_id=user_id)
    return {
        "report": format_bets_report(bets),
        "bets":   bets,
        "counts": {
            "stop_hit":    len([b for b in bets if b["status"] == "STOP_HIT"]),
            "near_stop":   len([b for b in bets if b["status"] == "NEAR_STOP"]),
            "on_track":    len([b for b in bets if b["status"] == "ON_TRACK"]),
            "near_target": len([b for b in bets if b["status"] == "NEAR_TARGET"]),
            "target_hit":  len([b for b in bets if b["status"] == "TARGET_HIT"]),
        }
    }

# ─────────────────────────────────────────────────────────────────────────────
# MARKET DATA TOOLS (W4)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_market_status() -> dict:
    """
    Current US market status — open/closed, last trading day, next open.
    Handles weekends and all US federal holidays for any year.
    Uses built-in NYSE trading calendar — no web search needed.
    """
    from app.scanner.quick_scan import (
        get_last_trading_date, _get_last_trading_session,
        _is_market_open, us_market_holidays,
    )
    today      = datetime.now()
    last_trade = get_last_trading_date()

    cursor = today.date()
    next_open = "unknown"
    for _ in range(10):
        cursor += timedelta(days=1)
        if cursor.weekday() < 5 and cursor not in us_market_holidays(cursor.year):
            next_open = str(cursor)
            break

    return {
        "today":            str(today.date()),
        "market_open":      _is_market_open(),
        "status":           _get_last_trading_session(),
        "last_trading_day": last_trade,
        "next_trading_day": next_open,
        "is_weekend":       today.weekday() >= 5,
        "is_holiday":       (today.date() in us_market_holidays(today.year)
                             and today.weekday() < 5),
    }


@mcp.tool()
def get_quote(ticker: str) -> dict | None:
    """Get previous trading day OHLCV quote for a ticker."""
    return get_previous_close(ticker.upper())


@mcp.tool()
def get_quotes(tickers: list[str]) -> dict[str, dict]:
    """Get previous day OHLCV quotes for multiple tickers at once."""
    return get_bulk_previous_close([t.upper() for t in tickers])


@mcp.tool()
def get_price_history(ticker: str, days: int = 200, timespan: str = "day") -> list[dict]:
    """
    Historical OHLCV bars for a ticker.
    Args:
        ticker:   e.g. 'NVDA'
        days:     calendar days back (default 200)
        timespan: 'minute' | 'hour' | 'day' | 'week' | 'month'
    """
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date   = datetime.now().strftime("%Y-%m-%d")
    return get_bars(ticker.upper(), 1, timespan, from_date, to_date)


@mcp.tool()
def get_ticker_info(ticker: str) -> dict | None:
    """Fundamental details for a ticker: name, market_cap, exchange, type."""
    return get_ticker_details(ticker.upper())


# ─────────────────────────────────────────────────────────────────────────────
# TECHNICAL ANALYSIS (W8)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def analyze_ticker(ticker: str, days: int = 300) -> dict:
    """
    Full technical analysis: MA20/50/200, EMA20, RSI(14), MACD,
    Bollinger Bands, ATR, support/resistance, trend, signal (BUY/SELL/NEUTRAL).
    Args:
        ticker: e.g. 'NVDA'
        days:   history days (default 300 for MA200)
    """
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date   = datetime.now().strftime("%Y-%m-%d")
    bars      = get_bars(ticker.upper(), 1, "day", from_date, to_date)
    return get_technical_profile(ticker.upper(), bars)


# ─────────────────────────────────────────────────────────────────────────────
# OPTIONS FLOW (W7)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_market_overview() -> dict:
    """
    Market-wide options flow: market tide, total volume, sector ETF flow,
    upcoming economic events. Shows macro direction of institutional money.
    """
    from app.options_flow.unusual_whales import (
        get_market_tide, get_total_options_volume,
        get_sector_etfs, get_economic_calendar,
    )
    from app.options_flow.signals import score_market_tide

    tide     = get_market_tide()
    sectors  = get_sector_etfs()
    bullish  = sorted(
        [s for s in sectors if float(s.get("bullish_premium") or 0) > float(s.get("bearish_premium") or 0)],
        key=lambda x: float(x.get("bullish_premium") or 0), reverse=True
    )[:3]
    bearish  = sorted(
        [s for s in sectors if float(s.get("bearish_premium") or 0) > float(s.get("bullish_premium") or 0)],
        key=lambda x: float(x.get("bearish_premium") or 0), reverse=True
    )[:3]

    return {
        "market_tide":             score_market_tide(tide),
        "total_options_volume":    get_total_options_volume(),
        "bullish_sectors":         [{"ticker": s["ticker"], "name": s.get("full_name")} for s in bullish],
        "bearish_sectors":         [{"ticker": s["ticker"], "name": s.get("full_name")} for s in bearish],
        "upcoming_economic_events": get_economic_calendar()[:10],
    }


@mcp.tool()
def get_options_flow(
    ticker: str | None = None,
    min_premium: float = 500000,
    sweeps_only: bool = False,
) -> list[dict]:
    """
    Recent options flow alerts (institutional sweeps).
    Args:
        ticker:       specific ticker or None for all market
        min_premium:  minimum premium $ (default $500K)
        sweeps_only:  only multi-exchange aggressive buys
    """
    from app.options_flow.unusual_whales import get_flow_alerts
    return get_flow_alerts(ticker=ticker, min_premium=min_premium, sweeps_only=sweeps_only)


@mcp.tool()
def get_dark_pool(ticker: str | None = None, min_premium: float = 0) -> list[dict]:
    """
    Dark pool institutional block trades.
    Args:
        ticker:       specific ticker or None for all recent
        min_premium:  minimum trade premium $
    """
    from app.options_flow.unusual_whales import get_dark_pool_ticker, get_dark_pool_recent
    if ticker:
        return get_dark_pool_ticker(ticker.upper())
    return get_dark_pool_recent(min_premium=min_premium)


@mcp.tool()
def get_gex(ticker: str) -> dict:
    """
    Dealer Gamma Exposure (GEX) for a ticker.
    Returns: net_gamma, gamma_wall (key price level), GEX by strike and expiry.
    Positive GEX = stabilizing. Negative GEX = volatile/accelerating moves.
    """
    from app.options_flow.unusual_whales import (
        get_greek_exposure, get_gex_by_strike, get_gex_by_expiry,
    )
    from app.options_flow.signals import score_gex

    gex        = get_greek_exposure(ticker)
    by_strike  = get_gex_by_strike(ticker)
    by_expiry  = get_gex_by_expiry(ticker)
    quote      = get_previous_close(ticker.upper())
    spot       = quote["close"] if quote else 0
    top_strikes = sorted(
        [s for s in by_strike if s.get("call_gex") or s.get("put_gex")],
        key=lambda x: abs(float(x.get("call_gex") or 0) + float(x.get("put_gex") or 0)),
        reverse=True
    )[:5]

    return {
        "ticker":          ticker.upper(),
        "current_price":   spot,
        "gex_score":       score_gex(gex, by_strike, spot),
        "top_gex_strikes": top_strikes,
        "gex_by_expiry":   by_expiry[:10],
    }


@mcp.tool()
def get_ticker_signal(ticker: str) -> dict:
    """
    Complete options flow signal for a ticker.
    Combines: flow sweeps, dark pool, GEX, market tide, earnings risk.
    Returns: direction, confidence (0-100), signal, summary.
    Call this before get_strategy_recommendation().
    """
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package
    return score_signal_package(get_signal_package(ticker.upper()))


@mcp.tool()
def get_earnings_calendar() -> dict:
    """Today's earnings — premarket and afterhours. Avoid new positions within 7 days."""
    from app.options_flow.unusual_whales import get_earnings_afterhours, get_earnings_premarket
    return {"premarket": get_earnings_premarket(), "afterhours": get_earnings_afterhours()}


@mcp.tool()
def get_news(ticker: str | None = None) -> list[dict]:
    """Recent news headlines with sentiment. ticker=None for all major news."""
    from app.options_flow.unusual_whales import get_news_headlines
    return get_news_headlines(ticker=ticker)


@mcp.tool()
def get_congress_trades(ticker: str | None = None) -> list[dict]:
    """Congressional stock trades. ticker=None for all recent trades."""
    from app.options_flow.unusual_whales import get_congress_trades as _get
    return _get(ticker=ticker)


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY ENGINE (W9)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_daily_recommendations(force_refresh: bool = False) -> dict:
    """
    Today's high-conviction trading thesis — the main daily recommendation.

    Call this when user says ANY of:
    - "What should I trade today?"
    - "Give me today's recommendations"
    - "What are the best picks today?"
    - "Show me today's thesis"

    Returns top 5 picks with conviction score (0-100), thesis, entry zone,
    target, stop, and invalidation conditions. Only surfaces picks >= 70/100.

    After returning, if any pick has act_now=True, ALWAYS ask:
    "Did you execute [ticker]? Tell me how many contracts at what price."

    Args:
        force_refresh: True to re-run scanner (default: use today's cached recs)
    """
    from app.recommendations.daily_engine import (
        run_daily_recommendations, format_daily_recommendations
    )
    result = run_daily_recommendations(
        user_id       = get_current_user_id(),
        force_refresh = force_refresh
    )
    result["formatted"] = format_daily_recommendations(result)
    return result


@mcp.tool()
def invalidate_recommendation(ticker: str, reason: str = "Manual invalidation") -> dict:
    """
    Mark today's recommendation for a ticker as invalidated (thesis broken).
    Fires Discord alert asking user to book profit or loss.

    Call when user says ANY of:
    - "The GOOGL thesis is broken"
    - "NVDA crossed my stop"
    - "Invalidate [ticker]"
    - "The [ticker] trade isn't working"
    """
    from app.recommendations.daily_engine import invalidate_recommendation as _inv
    success = _inv(get_current_user_id(), ticker.upper(), reason)
    return {
        "invalidated": success,
        "ticker":      ticker.upper(),
        "reason":      reason,
        "message":     f"Thesis invalidated for {ticker}. Discord alert sent — please review your position.",
    }


@mcp.tool()
def get_recommendation_history(days_back: int = 7) -> list[dict]:
    """
    Historical daily recommendations — see past thesis and outcomes.
    Shows what we recommended, conviction score, and current status.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT ticker, date, direction, conviction_score,
                       conviction_tier, thesis, target_pct, stop_pct,
                       status, invalidated_reason, strategy, risk_reward
                FROM daily_recommendations
                WHERE user_id = :uid
                  AND date >= CURRENT_DATE - :days
                ORDER BY date DESC, conviction_score DESC
            """), {"uid": get_current_user_id(), "days": days_back}).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]

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
    THE PRIMARY TRADE TOOL — scans all expiries and returns the optimal
    options trade recommendation for your budget and risk tolerance.

    Example output:
        NVDA DEBIT_PUT_SPREAD — BEARISH
        BUY $205P / SELL $195P — Jun 27 (10 DTE)
        8 contracts @ $1.85 = $1,480 total
        Target: +$2,368 (+160%) | Stop: -$592 (-40%) | R/R: 4.0

    Args:
        ticker:        e.g. 'NVDA', 'SPY', 'AMD'
        budget:        max capital in $ (default $2,000)
        max_loss:      max acceptable loss $ (default 40% of budget)
        profit_target: minimum target profit $ (filters expiries)
        min_dte:       minimum days to expiry (default 4)
        max_dte:       maximum days to expiry (default 365)

    ⚠️ Educational analysis only. Not financial advice.

    CRITICAL: After showing this recommendation, ALWAYS end with:
    "Did you execute this trade? Reply YES with the price you paid per
    contract and number of contracts, and I will log it and start
    monitoring your position every 15 minutes."
    """
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package
    from app.strategy.engine import build_recommendation

    ticker    = ticker.upper()
    from_date = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
    to_date   = datetime.now().strftime("%Y-%m-%d")
    bars      = get_bars(ticker, 1, "day", from_date, to_date)

    return build_recommendation(
        ticker        = ticker,
        ta_profile    = get_technical_profile(ticker, bars),
        flow_signal   = score_signal_package(get_signal_package(ticker)),
        budget        = budget,
        max_loss      = max_loss,
        profit_target = profit_target,
        min_dte       = min_dte,
        max_dte       = max_dte,
    )


# ─────────────────────────────────────────────────────────────────────────────
# WATCHLIST & SCANNER (W10, W11)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_watchlist() -> dict:
    """
    Returns your watchlist tickers from DB cache (instant, <100ms).
    Automatically syncs with live Webull account in the background —
    any adds/removes in Webull appear on the next call.
    Use force_sync_watchlist() for immediate Webull refresh.
    """
    from app.broker.watchlist_sync import get_watchlist_with_sync_status
    return get_watchlist_with_sync_status(get_current_user_id())


@mcp.tool()
def force_sync_watchlist() -> dict:
    """
    Force immediate sync between DB watchlist and live Webull account.
    Shows exactly what was added/removed.
    Use when you've made changes in Webull and want them reflected now.
    """
    from app.broker.watchlist_sync import force_sync
    result = force_sync(get_current_user_id())
    return {
        "synced":  True,
        "added":   result.get("added", []),
        "removed": result.get("removed", []),
        "total":   result.get("total", 0),
        "message": "Already up to date" if not result.get("added") and not result.get("removed")
                   else f"+{len(result.get('added',[]))} added, -{len(result.get('removed',[]))} removed",
    }


@mcp.tool()
def add_to_watchlist(ticker: str, notes: str = "", sector: str = "") -> dict:
    """
    Add a ticker to your watchlist.
    Args:
        ticker: e.g. 'NVDA'
        notes:  optional e.g. 'watching for breakout above 220'
        sector: optional e.g. 'Semiconductors'
    """
    from app.db.queries.watchlist import add_to_watchlist as _add
    added = _add(get_current_user_id(), ticker.upper(), notes, sector)
    return {
        "ticker":  ticker.upper(),
        "added":   added,
        "message": f"{'Added' if added else 'Already in'} watchlist",
    }


@mcp.tool()
def remove_from_watchlist(ticker: str) -> dict:
    """Remove a ticker from your watchlist."""
    from app.db.queries.watchlist import remove_from_watchlist as _remove
    removed = _remove(get_current_user_id(), ticker.upper())
    return {"ticker": ticker.upper(), "removed": removed}


@mcp.tool()
def get_scan_universe(
    extra_tickers: list[str] | None = None,
    min_market_cap: float = 0,
    sectors: list[str] | None = None,
    min_price: float = 0,
) -> list[str]:
    """
    Full ticker universe: watchlist + positions + optional filters.
    Args:
        extra_tickers:   always include these
        min_market_cap:  e.g. 10000000000 = $10B minimum
        sectors:         e.g. ['Technology', 'Semiconductors']
        min_price:       minimum stock price
    """
    from app.scanner.universe import get_scan_universe_mcp
    return get_scan_universe_mcp(
        extra_tickers=extra_tickers, min_market_cap=min_market_cap,
        sectors=sectors, min_price=min_price,
    )


@mcp.tool()
def scan_watchlist(top_n: int = 5) -> dict:
    """
    DAILY SCAN — Two-Tier Convergence Scanner on full 126-ticker watchlist.

    Tier 1 (~30s): Scores ALL 126 tickers on price momentum + options flow
    + dark pool. Selects top_n where signals CONVERGE.

    Tier 2 (~60-90s per ticker): Deep LLM strategy analysis on top picks.
    Running all 126 would take 190+ minutes — the pre-filter finds the
    highest-probability setups from the full universe.

    Args:
        top_n: picks to deep-analyze (default 5, max 15)
    """
    from app.scanner.quick_scan import run_full_scan
    return run_full_scan(top_quick=min(top_n, 15), user_id=get_current_user_id())


# ─────────────────────────────────────────────────────────────────────────────
# SELL SIGNALS + PORTFOLIO P&L (W5)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_portfolio_pnl() -> dict:
    """
    Full portfolio P&L snapshot: total value, cost, unrealized P&L,
    win rate, per-position breakdown sorted by P&L, best/worst performers.
    """
    from app.broker.sell_signals import get_portfolio_pnl_summary
    user_id = get_current_user_id()
    wb      = WebullConnector(user_id)
    pos     = wb.get_positions()
    try:    bal = wb.get_balance()
    except: bal = None
    return get_portfolio_pnl_summary(pos, bal)


@mcp.tool()
def get_sell_signals(use_llm: bool = True) -> dict:
    """
    Analyze all open positions for exit signals.

    Tier 1 (instant): Rule-based — stop loss (-40%), take profit (+20% stocks /
    +80% options), DTE warning, earnings risk, TA reversal (Polygon EMA/RSI).

    Tier 2 (LLM, ~25s): ALL positions analyzed in one batch call.
    LLM sees full context: P&L, TA, UW flow, sector peers, past signals.
    Checks sell_recommendations table — flags if signal was previously ignored.
    LLM can surface exits even for HOLD positions if context warrants it.

    Args:
        use_llm: True = full LLM analysis (default). False = instant rule-based only.
    """
    from app.broker.sell_signals import (
        evaluate_sell_signals,
        evaluate_sell_signals_with_llm,
        format_sell_report,
        get_portfolio_pnl_summary,
    )
    user_id = get_current_user_id()
    pos     = WebullConnector(user_id).get_positions()
    pnl     = get_portfolio_pnl_summary(pos, None)

    if use_llm:
        signals = evaluate_sell_signals_with_llm(pos, user_id=user_id)
    else:
        signals = evaluate_sell_signals(pos)

    return {
        "report":  format_sell_report(signals, pnl),
        "signals": signals,
        "pnl":     pnl,
    }

@mcp.tool()
def get_market_context(ticker: str) -> dict:
    """
    Full market context for any ticker — used automatically by scan_watchlist
    and get_sell_signals, but call directly to research before trading.

    Returns:
    - Price trend: 6-month performance, MA50/200, S/R levels, volume
    - Earnings: last 4 quarters (beat/miss + reaction %) + next upcoming
    - Macro calendar: Fed/CPI/NFP/PCE in next 30 days
    - Ticker news: Polygon AI-analyzed articles with sentiment per ticker
    - Global news: CNBC + MarketWatch + Federal Reserve + UW market headlines
    - Sector: sector ETF performance vs SPY (outperforming/underperforming)

    All data fetched fresh, session-cached for 1 hour.
    """
    from app.rag.context_builder import build_ticker_context
    ctx = build_ticker_context(ticker.upper())
    return {
        "ticker":          ctx["ticker"],
        "price":           ctx.get("price", {}),
        "earnings":        ctx.get("earnings", {}),
        "macro":           ctx.get("macro", {}),
        "ticker_news":     ctx.get("ticker_news", []),
        "global_news":     ctx.get("global_news", []),
        "sector":          ctx.get("sector", {}),
        "formatted":       ctx.get("formatted_prompt", ""),
        "build_time_s":    ctx.get("build_time_s"),
    }

# ─────────────────────────────────────────────────────────────────────────────
# POSITION MONITOR (W13)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def start_monitor() -> dict:
    """
    Start background position monitor — polls every 2 minutes during market hours.
    Fires alerts when positions hit stop loss, take profit, earnings risk, or DTE warning.
    Alerts stored in DB, readable via get_active_alerts().
    Also caches portfolio for instant reads (used by get_portfolio_pnl).
    """
    from app.monitor.position_monitor import get_monitor
    return get_monitor(get_current_user_id()).start()


@mcp.tool()
def stop_monitor() -> dict:
    """Stop the background position monitor."""
    from app.monitor.position_monitor import get_monitor
    return get_monitor(get_current_user_id()).stop()


@mcp.tool()
def get_monitor_status() -> dict:
    """
    Current monitor status: running, last check time, pending alerts,
    total checks run, total alerts fired.
    """
    from app.monitor.position_monitor import get_monitor
    return get_monitor(get_current_user_id()).status()


@mcp.tool()
def get_active_alerts(limit: int = 20) -> list[dict]:
    """
    Unread position alerts sorted by urgency (HIGH first).
    Each alert: symbol, type, urgency, message, P&L, triggered_at.
    Alert types: STOP_LOSS / TAKE_PROFIT / EARNINGS / DTE_WARNING / TA_REVERSAL / WATCH
    """
    from app.monitor.position_monitor import get_active_alerts as _get
    return _get(get_current_user_id(), limit=limit)


@mcp.tool()
def dismiss_alert(alert_id: str) -> dict:
    """Mark a specific alert as read and dismissed."""
    from app.monitor.position_monitor import dismiss_alert as _dismiss
    dismissed = _dismiss(get_current_user_id(), alert_id)
    return {"dismissed": dismissed, "alert_id": alert_id}


@mcp.tool()
def dismiss_all_alerts() -> dict:
    """Dismiss all pending alerts."""
    from app.monitor.position_monitor import dismiss_all_alerts as _dismiss_all
    count = _dismiss_all(get_current_user_id())
    return {"dismissed": count}

@mcp.tool()
def mute_alerts(symbol: str | None = None, hours: int | None = None) -> dict:
    """
    Stop alerts — globally or for a specific symbol.
    Examples:
      "stop all alerts"              → mute_alerts()
      "stop GLD alerts"              → mute_alerts(symbol='GLD')
      "mute GLD for 24 hours"        → mute_alerts(symbol='GLD', hours=24)
      "stop all alerts for 8 hours"  → mute_alerts(hours=8)
    Args:
        symbol: specific ticker to mute (None = mute everything)
        hours:  how long to mute (None = until you say unmute)
    """
    from app.monitor.position_monitor import mute_alerts as _mute
    return _mute(get_current_user_id(), symbol=symbol, hours=hours)


@mcp.tool()
def unmute_alerts(symbol: str | None = None) -> dict:
    """
    Re-enable alerts — globally or for a specific symbol.
    Args:
        symbol: specific ticker to unmute (None = unmute everything)
    """
    from app.monitor.position_monitor import unmute_alerts as _unmute
    return _unmute(get_current_user_id(), symbol=symbol)


@mcp.tool()
def get_mute_status() -> dict:
    """Show what's currently muted — global and per-symbol."""
    from app.monitor.position_monitor import get_mute_status as _status
    return _status(get_current_user_id())

# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATION (W14)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def configure_discord(webhook_url: str) -> dict:
    """
    Configure Discord notifications for trading alerts.
    Get webhook URL from: Discord channel → ⚙️ Settings → Integrations → Webhooks
    HIGH urgency alerts ping @here so your phone buzzes.
    MEDIUM urgency alerts post silently.
    LOW urgency = no notification.
    """
    from app.notifications.discord import save_webhook, send_test_notification
    saved = save_webhook(get_current_user_id(), webhook_url)
    if saved:
        return send_test_notification(get_current_user_id())
    return {"success": False, "error": "Failed to save webhook URL"}


@mcp.tool()
def test_notification() -> dict:
    """Send a test alert to Discord to verify notifications are working."""
    from app.notifications.discord import send_test_notification
    return send_test_notification(get_current_user_id())


@mcp.tool()
def get_notification_config() -> dict:
    """Show current notification settings — which channels configured, routing rules."""
    from app.notifications.discord import get_config
    return get_config(get_current_user_id())

# ─────────────────────────────────────────────────────────────────────────────
# PREDICTION TRACKER (W15)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def confirm_execution(
    symbol: str,
    entry_price: float,
    qty: int,
    recommendation_id: str | None = None,
) -> dict:
    """
    Call this when user says ANY of:
    - "I bought it", "I filled it", "I executed the trade"
    - "I placed the order", "I got filled", "position opened"
    - "I bought X contracts at $Y"

    Logs execution, links to recommendation, starts 15-min monitoring,
    sends Discord confirmation, adds to tracked_positions.

    Args:
        symbol:            ticker e.g. 'GOOGL'
        entry_price:      price paid per contract e.g. 8.50
        qty:         number of contracts e.g. 5
        recommendation_id: optional — links to specific recommendation
    """
    from app.learning.prediction_tracker import confirm_execution as _confirm
    return _confirm(get_current_user_id(), symbol, entry_price,
                    qty, recommendation_id)


@mcp.tool()
def log_outcome(
    symbol: str,
    exit_price: float,
    exit_reason: str = "MANUAL",
) -> dict:
    """
    Call this when user says ANY of:
    - "I sold X", "I closed X", "I exited X"
    - "X expired", "I let X expire"
    - "I sold X at $Y"
    - After seeing a POSITION_CLOSED alert asking what happened

    Calculates actual P&L vs entry, updates win rate, feeds learning engine.
    Args:
        symbol:      ticker e.g. 'NOW'
        exit_price:  price received per share/contract e.g. 93.29
        exit_reason: TAKE_PROFIT / STOP_LOSS / MANUAL / EXPIRED
    """
    from app.learning.prediction_tracker import log_outcome as _log
    return _log(get_current_user_id(), symbol, exit_price, exit_reason)


@mcp.tool()
def mark_sell_acted(symbol: str, exit_pct: int = 100) -> dict:
    """
    Confirm you acted on a sell recommendation.
    Updates sell_recommendations so we can track if our signals were right.
    Args:
        symbol:   ticker you sold e.g. 'GLD'
        exit_pct: percentage of position you exited (default 100)
    """
    from app.learning.prediction_tracker import mark_sell_acted as _mark
    return _mark(get_current_user_id(), symbol, exit_pct)


@mcp.tool()
def get_trade_history(limit: int = 20) -> list[dict]:
    """
    Full history of executed trades — open and closed.
    Shows entry, exit, actual P&L, and win/loss for each.
    """
    from app.learning.prediction_tracker import get_trade_history as _history
    return _history(get_current_user_id(), limit)

# ─────────────────────────────────────────────────────────────────────────────
# LEARNING (W16)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_learning_report() -> dict:
    """
    Full learning report — what is the platform learning from your trades?
    Shows: sell signal compliance, strategy win rates, weight adjustments,
    prioritised action items and behavioural insights.
    """
    from app.learning.engine import get_learning_report as _report
    return _report(get_current_user_id())

@mcp.tool()
def get_strategy_weights() -> dict:
    """
    Current strategy confidence weights based on your trade history.
    High win rate = boosted, low win rate = penalised.
    Applied automatically in get_strategy_recommendation().
    """
    from app.learning.engine import get_strategy_weights as _weights
    return _weights(get_current_user_id())

@mcp.tool()
def get_sell_signal_compliance() -> dict:
    """
    How often do you act on sell signals vs ignore them?
    Shows which symbols you repeatedly ignore and at what cost.
    """
    from app.learning.engine import analyze_sell_signal_compliance as _c
    return _c(get_current_user_id())

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()