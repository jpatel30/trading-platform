"""
Automated paper-trade-open job — runs once each morning, sweeps a grid
of trading windows and budget amounts for both options and stock, and
auto-confirms the highest-conviction pick PER COMBO (not one overall
winner per day) so Phase 6's weekly review has enough real, varied
outcome data to find out which window/amount actually correlates with
wins.

GRID (first-pass calibration guess, not a final tuned number):
    OPTIONS: windows [3, 7, 14, 30, 60] days x budgets [1000, 2500, 5000, 10000]  = 20 combos
    STOCK:   windows [14, 30, 90, 180, 365] days x budgets [1000, 2500, 5000, 10000] = 20 combos

Reuses rescan_with_validation() / run_smart_stock_scan() exactly - no
parallel scan logic. Budget does NOT change which ticker/strategy gets
picked (only the LLM prompt's one informational "Budget: $X" line for
options, and nothing at all for stock's composite-score ranking) - it
only changes position sizing. So for each window, the expensive part
(scan + enrichment + one LLM call for options; scan + composite scoring
for stock) runs exactly ONCE, at the grid's first (reference) budget.
The other 3 budgets in that window reuse the SAME picked ticker/
strategy/strikes and only re-run the cheap, deterministic trade-math
sizing step - roughly a 4x reduction in LLM calls and enrichment work
versus running all 20 combos independently.
"""
import json
from datetime import datetime, timezone

OPTIONS_WINDOWS = [3, 7, 14, 30, 60]
OPTIONS_BUDGETS = [1000, 2500, 5000, 10000]

STOCK_WINDOWS = [14, 30, 90, 180, 365]
STOCK_BUDGETS = [1000, 2500, 5000, 10000]


# ─────────────────────────────────────────────────────────────────────────────
# Shared context helpers - one lookup per window, reused across that
# window's 4 budget variants (same ticker, so the same context applies).
# ─────────────────────────────────────────────────────────────────────────────

def _market_context() -> dict:
    try:
        from app.rag.context_builder import _build_vix_context
        from app.signals.market_regime import get_full_market_regime
        vix    = _build_vix_context()
        regime = get_full_market_regime()
        return {"vix_zone": vix.get("zone"), "regime_bias": regime.get("overall_bias")}
    except Exception as e:
        print(f"[PaperTrade] Market context failed: {e}")
        return {"vix_zone": None, "regime_bias": None}


def _iv_context(ticker: str) -> tuple:
    """(current atm_iv, 5-day trend as % rate of change) from iv_history —
    the rate of change over the last 5 RECORDED days, not just current
    level. Returns (None, None) if there's no history yet for this
    ticker (e.g. the after-hours batch hasn't run for it yet)."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT recorded_at, atm_iv FROM iv_history
                WHERE ticker = :t AND atm_iv IS NOT NULL
                ORDER BY recorded_at DESC LIMIT 5
            """), {"t": ticker.upper()}).fetchall()
        if not rows:
            return None, None
        current = float(rows[0].atm_iv)
        if len(rows) < 5:
            return current, None
        oldest = float(rows[-1].atm_iv)
        trend  = round((current - oldest) / oldest * 100, 2) if oldest else None
        return current, trend
    except Exception as e:
        print(f"[PaperTrade] IV context failed for {ticker}: {e}")
        return None, None


def _daily_snapshot_context(ticker: str) -> dict:
    """Today's TA from Phase 2's ticker_daily_snapshot (after-hours batch)."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text("""
                SELECT rsi14, macd_signal, trend FROM ticker_daily_snapshot
                WHERE ticker = :t AND date = CURRENT_DATE
            """), {"t": ticker.upper()}).fetchone()
        if not row:
            return {"rsi": None, "macd_signal": None, "trend": None}
        return {
            "rsi":         float(row.rsi14) if row.rsi14 is not None else None,
            "macd_signal": float(row.macd_signal) if row.macd_signal is not None else None,
            "trend":       row.trend,
        }
    except Exception as e:
        print(f"[PaperTrade] Daily snapshot context failed for {ticker}: {e}")
        return {"rsi": None, "macd_signal": None, "trend": None}


def _intraday_context(ticker: str, direction: str) -> dict:
    from app.signals.intraday_entry import get_intraday_signal
    return {
        "5min":  get_intraday_signal(ticker, direction, "5min"),
        "15min": get_intraday_signal(ticker, direction, "15min"),
    }


def _lookup_recommendation_id(user_id: str, ticker: str, horizon: str) -> str | None:
    from sqlalchemy import text
    from app.db.session import get_session
    with get_session() as s:
        row = s.execute(text("""
            SELECT id FROM daily_recommendations
            WHERE user_id = :uid AND ticker = :t AND date = CURRENT_DATE AND horizon = :h
            ORDER BY created_at DESC LIMIT 1
        """), {"uid": user_id, "t": ticker, "h": horizon}).fetchone()
        return str(row.id) if row else None


def _store_paper_context(
    recommendation_id: str | None, tracked_position_id: str | None,
    ticker: str, rec_type: str, window: int, budget: float,
    flow_score, dp_score, oi_score, oi_max_days,
    iv_level, iv_trend, daily_ctx: dict, intraday: dict,
    market_ctx: dict, conviction_score, strategy_selected: str, strategy_rule: str,
) -> None:
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                INSERT INTO paper_trade_context (
                    recommendation_id, tracked_position_id, ticker, rec_type,
                    trading_window_days, budget_swept,
                    flow_score, dp_score, oi_score, oi_max_days,
                    iv_level, iv_5day_trend,
                    daily_rsi, daily_macd_signal, daily_trend,
                    intraday_5min_signal, intraday_15min_signal,
                    vix_zone, vix_regime_bias,
                    conviction_score, strategy_selected, which_strategy_rule_fired
                ) VALUES (
                    :rec_id, :tp_id, :ticker, :rec_type,
                    :window, :budget,
                    :flow, :dp, :oi, :oi_days,
                    :iv_level, :iv_trend,
                    :d_rsi, :d_macd, :d_trend,
                    CAST(:intra5 AS jsonb), CAST(:intra15 AS jsonb),
                    :vix_zone, :vix_bias,
                    :conv, :strat, :rule
                )
            """), {
                "rec_id": recommendation_id, "tp_id": tracked_position_id,
                "ticker": ticker, "rec_type": rec_type,
                "window": window, "budget": budget,
                "flow": flow_score, "dp": dp_score, "oi": oi_score, "oi_days": oi_max_days,
                "iv_level": iv_level, "iv_trend": iv_trend,
                "d_rsi": daily_ctx.get("rsi"), "d_macd": daily_ctx.get("macd_signal"),
                "d_trend": daily_ctx.get("trend"),
                "intra5": json.dumps(intraday.get("5min")), "intra15": json.dumps(intraday.get("15min")),
                "vix_zone": market_ctx.get("vix_zone"), "vix_bias": market_ctx.get("regime_bias"),
                "conv": conviction_score, "strat": strategy_selected, "rule": strategy_rule,
            })
    except Exception as e:
        print(f"[PaperTrade] paper_trade_context write failed for {ticker}: {e}")


def _log_job_run(job_name: str, started_at, status: str, processed: int, failed: int, details: dict) -> None:
    """Same job_run_log table Phase 2's after-hours batch uses - the one
    place every scheduled job logs to."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                INSERT INTO job_run_log (
                    job_name, started_at, completed_at, status,
                    tickers_processed, tickers_failed, error_summary, details
                ) VALUES (
                    :job_name, :started_at, :completed_at, :status,
                    :processed, :failed, :error_summary, CAST(:details AS jsonb)
                )
            """), {
                "job_name": job_name, "started_at": started_at,
                "completed_at": datetime.now(timezone.utc), "status": status,
                "processed": processed, "failed": failed,
                "error_summary": details.get("error_summary"),
                "details": json.dumps(details),
            })
    except Exception as e:
        print(f"[PaperTrade] job_run_log write failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Options grid
# ─────────────────────────────────────────────────────────────────────────────

def _resize_options_trade(top_pick: dict, ticker: str, new_budget: float) -> dict | None:
    """
    Re-run ONLY the trade-math sizing step for a new budget, reusing the
    SAME LLM-selected ticker/strategy/strikes from top_pick - this is
    what avoids a second scan+enrich+LLM call per budget variant.
    """
    from app.strategy.engine import _execute_trade_math, normalize_strategy
    from app.options_flow.unusual_whales import get_stock_state

    strategy = normalize_strategy(top_pick.get("strategy", ""))
    legs_in  = [{"action": l["action"], "type": l["type"], "strike": l["strike"]}
                for l in top_pick.get("legs", [])]
    decision = {
        "strategy": strategy, "expiry": top_pick.get("expiry", ""),
        "dte": top_pick.get("dte", 0), "legs": legs_in,
        "direction": top_pick.get("direction", "NEUTRAL"),
        "confidence": top_pick.get("confidence", 65),
        "reasoning": top_pick.get("reasoning", ""), "key_risk": top_pick.get("key_risk", ""),
        "key_news": top_pick.get("key_news", "NONE"), "regime_check": "PASS",
    }
    try:
        state = get_stock_state(ticker)
        spot  = float(state.get("price", 0)) if state else 0
        if not spot:
            import yfinance as yf
            spot = yf.Ticker(ticker).fast_info.last_price or 0
        if not spot:
            return None

        max_loss = new_budget * 0.40
        trade = _execute_trade_math(decision, ticker, spot, new_budget, max_loss)
        trade["ticker"]        = ticker
        trade["direction"]     = decision["direction"]
        trade["reasoning"]     = decision["reasoning"]
        trade["key_risk"]      = decision["key_risk"]
        trade["confidence"]    = decision["confidence"]
        trade["strategy_rule"] = top_pick.get("strategy_rule", "")
        trade["flow_score"]    = top_pick.get("flow_score", 0)
        trade["dp_score"]      = top_pick.get("dp_score", 0)
        trade["oi_score"]      = top_pick.get("oi_score", 0)
        trade["oi_max_days"]   = top_pick.get("oi_max_days", 0)
        trade["iv_current"]    = top_pick.get("iv_current", 0)
        return trade
    except Exception as e:
        print(f"[PaperTrade] Resize failed for {ticker} @ ${new_budget}: {e}")
        return None


def _store_options_recommendation(user_id: str, window: int, budget: float, trade: dict, market_view: str) -> str | None:
    from app.recommendations.daily_engine import _upsert_recommendation

    entry_basis = abs(trade.get("entry_debit", 0))
    return _upsert_recommendation(user_id, {
        "ticker": trade["ticker"], "horizon": f"{window}d_{int(budget)}",
        "direction": trade.get("direction", ""),
        "conviction_score": trade.get("confidence", 65),
        "conviction_tier": "HIGH" if trade.get("confidence", 0) >= 75 else "MODERATE",
        "act_now": trade.get("confidence", 0) >= 70, "position_size_guidance": "standard",
        "thesis": trade.get("reasoning", ""),
        "entry_zone_low": entry_basis, "entry_zone_high": entry_basis * 1.05,
        "entry_trigger": "AT_MARKET",
        "target_price": round(entry_basis * 1.5, 2), "target_pct": 50.0,
        "stop_price": round(entry_basis * 0.6, 2), "stop_pct": -40.0,
        "timeframe": f"{window} days",
        "invalidation_conditions": trade.get("key_risk", ""),
        "strategy": trade.get("strategy", ""), "expiry": trade.get("expiry", ""),
        "dte": trade.get("dte", window), "legs": trade.get("legs", []),
        "entry_debit": trade.get("entry_debit", 0),
        "webull_limit_price": trade.get("webull_limit_price", 0),
        "total_cost": trade.get("total_cost", 0),
        "max_profit": trade.get("max_profit_per_contract", 0),
        "max_loss": trade.get("max_loss_per_contract", 0),
        "risk_reward": trade.get("risk_reward", 0),
        "webull_instructions": trade.get("webull_instructions", ""),
        "key_news": trade.get("key_risk", "NONE"), "warnings": trade.get("engine_warnings", []),
        "conviction_breakdown": {},
        "signal_data": {"market_view": market_view, "paper_trade": True},
    })


def run_paper_trade_open_options(user_id: str) -> dict:
    from app.recommendations.rescan_engine import rescan_with_validation
    from app.learning.prediction_tracker import confirm_execution

    started_at    = datetime.now(timezone.utc)
    market_ctx    = _market_context()
    combo_results = []
    confirmed     = 0
    errored       = 0

    for window in OPTIONS_WINDOWS:
        try:
            scan_result = rescan_with_validation(
                user_id=user_id, budget=OPTIONS_BUDGETS[0], trading_window_days=window,
            )
        except Exception as e:
            errored += len(OPTIONS_BUDGETS)
            for budget in OPTIONS_BUDGETS:
                combo_results.append({"window": window, "budget": budget, "outcome": "error", "detail": str(e)})
            continue

        if scan_result.get("error"):
            for budget in OPTIONS_BUDGETS:
                combo_results.append({"window": window, "budget": budget, "outcome": "empty",
                                       "detail": scan_result["error"]})
            continue

        picks = [p for p in scan_result.get("picks", []) if p.get("legs")]
        if not picks:
            for budget in OPTIONS_BUDGETS:
                combo_results.append({"window": window, "budget": budget, "outcome": "empty",
                                       "detail": "no qualifying picks (gates rejected everything)"})
            continue

        top_pick = picks[0]   # highest-conviction - final[] is already sorted
        ticker   = top_pick["ticker"]
        direction = top_pick.get("direction", "NEUTRAL")

        # Shared per-window context - same ticker for all 4 budgets, so
        # fetched once and reused rather than 4x.
        iv_level, iv_trend = _iv_context(ticker)
        daily_ctx  = _daily_snapshot_context(ticker)
        intraday   = _intraday_context(ticker, direction)

        for budget in OPTIONS_BUDGETS:
            try:
                # Always recompute trade math fresh for THIS budget via
                # _resize_options_trade, for every budget including the
                # first — top_pick itself isn't a reliable source of
                # contracts/entry_debit: if rescan_with_validation reloaded
                # this window's tickers as an already-existing "morning
                # pick" (e.g. a second run the same day, or a real user's
                # own scan earlier that morning), top_pick is shaped like a
                # _load_todays_recs() row, which has no "contracts" field
                # at all. Recomputing fresh avoids depending on which shape
                # top_pick happens to be.
                trade = _resize_options_trade(top_pick, ticker, budget)
                if not trade:
                    combo_results.append({"window": window, "budget": budget, "outcome": "empty",
                                           "detail": "trade-math gate rejected at this budget"})
                    continue

                entry_price = trade.get("entry_debit", 0)
                qty         = trade.get("contracts", 0)
                if not qty:
                    combo_results.append({"window": window, "budget": budget, "outcome": "empty",
                                           "detail": "zero contracts sized"})
                    continue

                # Reuse the recommendation row rescan_with_validation already
                # stored for the reference budget (bare "{window}d" horizon,
                # no budget suffix) if it's really there; otherwise (or for
                # every other budget) store our own budget-suffixed row so
                # each combo always has a real recommendation_id.
                rec_id = None
                if budget == OPTIONS_BUDGETS[0]:
                    rec_id = _lookup_recommendation_id(user_id, ticker, f"{window}d")
                if not rec_id:
                    rec_id = _store_options_recommendation(user_id, window, budget, trade, scan_result.get("market_view", ""))

                confirm_result = confirm_execution(
                    user_id=user_id, symbol=ticker, entry_price=entry_price, qty=qty,
                    recommendation_id=rec_id, source="auto_paper",
                )
                if not confirm_result.get("confirmed") and confirm_result.get("status") != "already_tracked":
                    errored += 1
                    combo_results.append({"window": window, "budget": budget, "outcome": "error",
                                           "detail": confirm_result.get("error", "confirm_execution did not confirm")})
                    continue
                tracked_position_id = confirm_result.get("tracked_position_id") or confirm_result.get("id")

                _store_paper_context(
                    recommendation_id=rec_id, tracked_position_id=tracked_position_id,
                    ticker=ticker, rec_type="options", window=window, budget=budget,
                    flow_score=trade.get("flow_score"), dp_score=trade.get("dp_score"),
                    oi_score=trade.get("oi_score"), oi_max_days=trade.get("oi_max_days"),
                    iv_level=iv_level, iv_trend=iv_trend, daily_ctx=daily_ctx, intraday=intraday,
                    market_ctx=market_ctx, conviction_score=trade.get("confidence"),
                    strategy_selected=trade.get("strategy"), strategy_rule=trade.get("strategy_rule", ""),
                )

                confirmed += 1
                combo_results.append({"window": window, "budget": budget, "outcome": "confirmed",
                                       "ticker": ticker, "strategy": trade.get("strategy")})
            except Exception as e:
                errored += 1
                combo_results.append({"window": window, "budget": budget, "outcome": "error", "detail": str(e)})
                print(f"[PaperTrade] Options combo window={window} budget={budget} failed: {e}")

    empty_count = sum(1 for c in combo_results if c["outcome"] == "empty")
    status = "success" if errored == 0 else ("partial" if confirmed > 0 else "failed")
    _log_job_run(
        "paper_trade_open_options", started_at, status, confirmed, errored,
        {"combos": combo_results, "confirmed": confirmed, "empty": empty_count, "errored": errored,
         "error_summary": None if errored == 0 else f"{errored} combo(s) errored"},
    )
    return {"job": "paper_trade_open_options", "confirmed": confirmed, "empty": empty_count,
            "errored": errored, "combos": combo_results, "status": status}


# ─────────────────────────────────────────────────────────────────────────────
# Stock grid
# ─────────────────────────────────────────────────────────────────────────────

def _resize_stock_trade(top_pick: dict, new_budget: float) -> dict:
    """
    Budget only affects shares/total_cost/potential_gain/potential_loss
    in get_stock_for_horizon()'s output - simple arithmetic, no need to
    re-call anything for the other 3 budgets in a window.
    """
    trade = dict(top_pick)
    current_price = trade.get("current_price", 0) or 0
    target_price  = trade.get("target_price", 0) or 0
    stop_price    = trade.get("stop_price", 0) or 0
    shares = max(1, int(new_budget / current_price)) if current_price else 1
    trade["shares"]         = shares
    trade["total_cost"]     = round(shares * current_price, 2)
    trade["potential_gain"] = round((target_price - current_price) * shares, 2)
    trade["potential_loss"] = round((stop_price - current_price) * shares, 2)
    return trade


def _store_stock_recommendation(user_id: str, window: int, budget: float, trade: dict, horizon_label: str) -> str | None:
    from app.recommendations.daily_engine import _upsert_recommendation

    return _upsert_recommendation(user_id, {
        "ticker": trade["ticker"], "horizon": f"{window}d_{int(budget)}",
        "direction": trade.get("direction", "BULLISH"),
        "conviction_score": trade.get("fundamental_score", 65),
        "conviction_tier": "HIGH" if trade.get("fundamental_score", 0) >= 75 else "MODERATE",
        "act_now": True, "position_size_guidance": "standard",
        "thesis": trade.get("thesis", ""),
        "entry_zone_low": trade.get("entry_price", 0), "entry_zone_high": trade.get("entry_price", 0),
        "entry_trigger": "AT_MARKET",
        "target_price": trade.get("target_price", 0), "target_pct": trade.get("target_pct", 0),
        "stop_price": trade.get("stop_price", 0), "stop_pct": trade.get("stop_pct", -15),
        "timeframe": horizon_label,
        "invalidation_conditions": trade.get("invalidation_conditions", ""),
        "strategy": "STOCK", "legs": [],
        "key_news": "NONE", "warnings": [],
        "conviction_breakdown": {},
        "signal_data": {"rec_type": "stock", "paper_trade": True},
    })


def run_paper_trade_open_stocks(user_id: str) -> dict:
    from app.recommendations.smart_stock_scan import run_smart_stock_scan
    from app.recommendations.horizon_engine import get_stock_for_horizon
    from app.learning.prediction_tracker import confirm_execution

    started_at    = datetime.now(timezone.utc)
    market_ctx    = _market_context()
    combo_results = []
    confirmed     = 0
    errored       = 0

    # Phase 1 (composite fundamentals+velocity+insider ranking) is
    # window-independent — run ONCE for the whole grid, not once per
    # window. Confirmed live: running it 5x back-to-back (131 tickers'
    # worth of yfinance/EDGAR calls each, within under a minute) hit real
    # rate limiting — every window after the first came back with zero
    # candidates at Phase 1 itself, not a gate rejection. Only Phase 2
    # (get_stock_for_horizon's target/stop, which DOES vary by window)
    # needs to re-run per window, and only for the one top-ranked ticker.
    try:
        ranking_result = run_smart_stock_scan(user_id, horizon="6m", budget=STOCK_BUDGETS[0], top_n=5)
    except Exception as e:
        errored = len(STOCK_WINDOWS) * len(STOCK_BUDGETS)
        for window in STOCK_WINDOWS:
            for budget in STOCK_BUDGETS:
                combo_results.append({"window": window, "budget": budget, "outcome": "error", "detail": str(e)})
        _log_job_run("paper_trade_open_stocks", started_at, "failed", 0, errored,
                     {"combos": combo_results, "error_summary": str(e)})
        return {"job": "paper_trade_open_stocks", "confirmed": 0, "empty": 0,
                "errored": errored, "combos": combo_results, "status": "failed"}

    ranked = ranking_result.get("stocks", [])
    if not ranked:
        for window in STOCK_WINDOWS:
            for budget in STOCK_BUDGETS:
                combo_results.append({"window": window, "budget": budget, "outcome": "empty",
                                       "detail": "no qualifying picks (fundamental gate rejected everything)"})
        _log_job_run("paper_trade_open_stocks", started_at, "success", 0, 0,
                     {"combos": combo_results, "confirmed": 0, "empty": len(combo_results), "errored": 0})
        return {"job": "paper_trade_open_stocks", "confirmed": 0, "empty": len(combo_results),
                "errored": 0, "combos": combo_results, "status": "success"}

    top_ranked    = ranked[0]
    ticker        = top_ranked["ticker"]
    current_price = top_ranked.get("current_price", 0)

    for window in STOCK_WINDOWS:
        try:
            top_pick = get_stock_for_horizon(
                ticker, "6m", STOCK_BUDGETS[0], current_price=current_price,
                trading_window_days=window,
            )
        except Exception as e:
            errored += len(STOCK_BUDGETS)
            for budget in STOCK_BUDGETS:
                combo_results.append({"window": window, "budget": budget, "outcome": "error", "detail": str(e)})
            continue

        if not top_pick or top_pick.get("filtered") or top_pick.get("error"):
            reason = (top_pick or {}).get("reason") or (top_pick or {}).get("error") or "filtered"
            for budget in STOCK_BUDGETS:
                combo_results.append({"window": window, "budget": budget, "outcome": "empty", "detail": reason})
            continue

        direction = top_pick.get("direction", "BULLISH")
        iv_level, iv_trend = _iv_context(ticker)
        daily_ctx  = _daily_snapshot_context(ticker)
        intraday   = _intraday_context(ticker, direction)

        for budget in STOCK_BUDGETS:
            try:
                trade = top_pick if budget == STOCK_BUDGETS[0] else _resize_stock_trade(top_pick, budget)

                rec_id = _store_stock_recommendation(user_id, window, budget, trade, f"{window} days")

                entry_price = trade.get("current_price", 0)
                qty         = trade.get("shares", 0)
                if not qty or not entry_price:
                    combo_results.append({"window": window, "budget": budget, "outcome": "empty",
                                           "detail": "zero shares sized"})
                    continue

                confirm_result = confirm_execution(
                    user_id=user_id, symbol=ticker, entry_price=entry_price, qty=qty,
                    recommendation_id=rec_id, source="auto_paper",
                )
                if not confirm_result.get("confirmed") and confirm_result.get("status") != "already_tracked":
                    errored += 1
                    combo_results.append({"window": window, "budget": budget, "outcome": "error",
                                           "detail": confirm_result.get("error", "confirm_execution did not confirm")})
                    continue
                tracked_position_id = confirm_result.get("tracked_position_id") or confirm_result.get("id")

                _store_paper_context(
                    recommendation_id=rec_id, tracked_position_id=tracked_position_id,
                    ticker=ticker, rec_type="stock", window=window, budget=budget,
                    flow_score=None, dp_score=trade.get("dp_score"),
                    oi_score=None, oi_max_days=None,
                    iv_level=iv_level, iv_trend=iv_trend, daily_ctx=daily_ctx, intraday=intraday,
                    market_ctx=market_ctx, conviction_score=trade.get("fundamental_score"),
                    strategy_selected="STOCK", strategy_rule="",
                )

                confirmed += 1
                combo_results.append({"window": window, "budget": budget, "outcome": "confirmed", "ticker": ticker})
            except Exception as e:
                errored += 1
                combo_results.append({"window": window, "budget": budget, "outcome": "error", "detail": str(e)})
                print(f"[PaperTrade] Stock combo window={window} budget={budget} failed: {e}")

    empty_count = sum(1 for c in combo_results if c["outcome"] == "empty")
    status = "success" if errored == 0 else ("partial" if confirmed > 0 else "failed")
    _log_job_run(
        "paper_trade_open_stocks", started_at, status, confirmed, errored,
        {"combos": combo_results, "confirmed": confirmed, "empty": empty_count, "errored": errored,
         "error_summary": None if errored == 0 else f"{errored} combo(s) errored"},
    )
    return {"job": "paper_trade_open_stocks", "confirmed": confirmed, "empty": empty_count,
            "errored": errored, "combos": combo_results, "status": status}


# ─────────────────────────────────────────────────────────────────────────────
# Close job — the other half of Phase 4. Without this, every auto_paper
# position opened in the morning stays open forever and never produces a
# win/loss data point for Phase 6 to learn from.
# ─────────────────────────────────────────────────────────────────────────────

def run_paper_trade_close(user_id: str) -> dict:
    """
    Closes every auto_paper position opened TODAY, at end of day, computes
    real P&L, and writes the SAME daily_recommendations columns a real
    fill's exit uses (exit_price, actual_pnl, actual_pnl_pct, was_correct,
    closed_at) so Phase 6's weekly review can query paper trades exactly
    like real ones — no special-casing downstream.

    Deliberately does NOT go through log_exit() — that function (a) looks
    up "the most recently-entered active position for this symbol",
    which is ambiguous the moment more than one window/budget combo is
    still open on the same ticker (the grid's whole point), and (b)
    computes pnl as a plain (exit_price - entry_price) / entry_price,
    which mis-prices any credit strategy (IRON_CONDOR etc. store
    entry_debit negative) the exact same way mark_to_market's own
    pnl_pct was wrong before that was fixed. Instead, each position is
    closed by its own tracked_positions.id, matched to its exact
    daily_recommendations row via paper_trade_context (written by the
    open job), and priced with mark_to_market.py's own debit/credit-aware
    mark_recommendation() — reusing its fetch functions
    (get_current_option_value / get_current_stock_value) AND its P&L
    math, not just the fetch.
    """
    from sqlalchemy import text
    from app.db.session import get_session
    from app.recommendations.mark_to_market import mark_recommendation

    started_at        = datetime.now(timezone.utc)
    results            = []
    closed             = 0
    wins               = 0
    losses             = 0
    errored            = 0
    total_pnl_dollars  = 0.0

    with get_session() as s:
        rows = s.execute(text("""
            SELECT tp.id AS tp_id, tp.symbol, tp.qty,
                   ptc.recommendation_id,
                   dr.legs, dr.entry_debit, dr.entry_zone_low,
                   dr.max_loss, dr.max_profit
            FROM tracked_positions tp
            LEFT JOIN paper_trade_context ptc ON ptc.tracked_position_id = tp.id
            LEFT JOIN daily_recommendations dr ON dr.id = ptc.recommendation_id
            WHERE tp.source = 'auto_paper'
              AND tp.is_active = TRUE
              AND tp.entry_date = CURRENT_DATE
        """)).fetchall()

    for row in rows:
        ticker = row.symbol
        qty    = int(row.qty or 0)
        try:
            if not row.recommendation_id:
                errored += 1
                results.append({"tp_id": str(row.tp_id), "ticker": ticker, "outcome": "error",
                                 "detail": "no paper_trade_context/recommendation link found"})
                continue

            rec = {
                "ticker":         ticker,
                "legs":           row.legs or [],
                "entry_debit":    float(row.entry_debit or 0),
                "entry_zone_low": float(row.entry_zone_low or 0),
                "max_loss":       float(row.max_loss or 0),
                "max_profit":     float(row.max_profit or 0),
            }
            mark = mark_recommendation(rec, is_market_open=True)

            if mark["current_value"] is None or mark["pnl_pct"] is None:
                errored += 1
                results.append({"tp_id": str(row.tp_id), "ticker": ticker, "outcome": "error",
                                 "detail": "could not mark to market (contract/quote unavailable)"})
                continue

            # mark_recommendation's pnl_dollars is per ONE contract (options)
            # or per ONE share (stock) — scale by the real position size to
            # get the actual realized dollar P&L for this combo.
            realized_pnl_dollars = round(mark["pnl_dollars"] * qty, 2)
            pnl_pct    = mark["pnl_pct"]
            exit_price = mark["current_value"]
            won        = pnl_pct > 0

            with get_session() as s2:
                s2.execute(text("""
                    UPDATE tracked_positions
                    SET is_active   = FALSE,
                        exit_date   = CURRENT_DATE,
                        exit_price  = :ep,
                        exit_reason = :reason
                    WHERE id = :id
                """), {"ep": exit_price, "reason": "EOD_AUTO_CLOSE", "id": row.tp_id})

                s2.execute(text("""
                    UPDATE daily_recommendations
                    SET exit_price     = :ep,
                        exit_reason    = :reason,
                        closed_at      = now(),
                        actual_pnl     = :pnl_abs,
                        actual_pnl_pct = :pnl_pct,
                        was_correct    = :won
                    WHERE id = :rid AND closed_at IS NULL
                """), {
                    "ep": exit_price, "reason": "EOD_AUTO_CLOSE",
                    "pnl_abs": realized_pnl_dollars, "pnl_pct": pnl_pct, "won": won,
                    "rid": row.recommendation_id,
                })

            closed += 1
            total_pnl_dollars += realized_pnl_dollars
            if won:
                wins += 1
            else:
                losses += 1
            results.append({
                "tp_id": str(row.tp_id), "ticker": ticker, "outcome": "closed",
                "exit_price": exit_price, "pnl_dollars": realized_pnl_dollars,
                "pnl_pct": pnl_pct, "won": won,
            })
        except Exception as e:
            errored += 1
            results.append({"tp_id": str(row.tp_id), "ticker": ticker, "outcome": "error", "detail": str(e)})
            print(f"[PaperTrade] Close failed for {ticker} ({row.tp_id}): {e}")

    win_rate = round(wins / closed * 100, 1) if closed else None
    status = "success" if errored == 0 else ("partial" if closed > 0 else "failed")
    _log_job_run(
        "paper_trade_close", started_at, status, closed, errored,
        {"results": results, "closed": closed, "wins": wins, "losses": losses,
         "win_rate": win_rate, "total_pnl_dollars": round(total_pnl_dollars, 2),
         "error_summary": None if errored == 0 else f"{errored} position(s) errored"},
    )
    return {
        "job": "paper_trade_close", "closed": closed, "wins": wins, "losses": losses,
        "win_rate": win_rate, "total_pnl_dollars": round(total_pnl_dollars, 2),
        "errored": errored, "results": results, "status": status,
    }
