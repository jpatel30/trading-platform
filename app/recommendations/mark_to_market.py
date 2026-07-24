"""
Mark-to-Market Engine.

Calculates current value of stored recommendations (options + stocks)
for the History tab and backtest dataset.

Lazy-refresh model: triggered when History tab loads, only re-marks
if existing marks are >15 min stale. No separate scheduler needed.

Rewritten July 2026 — found the P&L% denominator was wrong for credit
strategies (iron condor etc). pnl_pct was computed as profit/credit
received, but credit received is NOT the capital at risk for a credit
trade — max_loss is. This inflated every condor's return by roughly
(max_loss/credit), e.g. ~3.26x for a QQQ condor tested this session
(credit=$470, max_loss=$1,530). Debit trades were always correct
(entry_debit = premium paid = capital at risk there, so no fix needed
for that branch). Also added excluded_from_stats filtering so rows
flagged as corrupted (phantom $1M+ "max profit" from a fixed upstream
bug) no longer pollute backtest aggregates.
"""
from datetime import datetime, timezone


def get_current_option_value(ticker: str, legs: list[dict]) -> float | None:
    """
    Fetch current value of an option spread given ticker + stored legs.
    Matches each leg's strike+type to live contract data from same expiry.
    Returns current cost-to-enter this exact spread (same BUY/SELL convention
    as original entry_debit calculation) — None if any leg can't be matched.
    """
    if not legs or not ticker:
        return None

    try:
        from app.options_flow.unusual_whales import get_option_contracts

        expiry = legs[0].get("expiry", "")
        if not expiry:
            return None

        # limit=500 — a single expiry's full chain (SPY confirmed 334
        # contracts one expiry) can exceed the old limit=200, which
        # silently dropped deep ITM/OTM strikes (found live: a 3-day SPY
        # iron condor's wings weren't in the first 200 and every leg
        # match failed, so the whole position could never be marked).
        contracts = get_option_contracts(ticker, expiry=expiry, limit=500)
        if not contracts:
            return None

        contract_map = {}
        for c in contracts:
            sym = c.get("option_symbol", "")
            bid = float(c.get("nbbo_bid", 0) or c.get("bid", 0) or 0)
            ask = float(c.get("nbbo_ask", 0) or c.get("ask", 0) or 0)
            # A deep OTM leg with bid=0/ask=0.01 is real and near-worthless,
            # not "no data" — the old `bid and ask` check treated that as
            # unmatchable and skipped it, which breaks ANY spread with a
            # near-worthless wing (the common case for iron condor wings
            # once the position is winning). Only fall back to mid/last_price
            # when there's truly no quote on either side.
            if bid or ask:
                mid = (bid + ask) / 2
            else:
                mid = float(c.get("mid", 0) or c.get("last_price", 0) or 0)
            if not sym or (bid == 0 and ask == 0 and mid == 0):
                continue
            try:
                for marker in ("C", "P"):
                    idx = sym.rfind(marker)
                    if idx > 0 and sym[idx+1:].isdigit() and len(sym[idx+1:]) == 8:
                        strike = int(sym[idx+1:]) / 1000.0
                        contract_map[(strike, marker)] = mid
                        break
            except Exception:
                continue

        def _find_match(strike, type_key):
            if (strike, type_key) in contract_map:
                return contract_map[(strike, type_key)]
            for (s, t), m in contract_map.items():
                if t == type_key and abs(s - strike) < 0.01:
                    return m
            return None

        total_value = 0.0
        matched     = 0
        for leg in legs:
            strike   = round(float(leg.get("strike", 0) or 0), 2)
            type_key = (leg.get("type", "")).upper()[:1]
            action   = leg.get("action", "")

            current_mid = _find_match(strike, type_key)
            if current_mid is None:
                continue
            matched += 1

            if action == "BUY":
                total_value += current_mid
            elif action == "SELL":
                total_value -= current_mid

        if matched < len(legs):
            return None

        return round(total_value, 2)

    except Exception as e:
        print(f"[MarkToMarket] Option value error for {ticker}: {e}")
        return None


def get_current_stock_value(ticker: str) -> float | None:
    """Get current stock price for mark-to-market."""
    try:
        from app.options_flow.unusual_whales import get_stock_state
        state = get_stock_state(ticker)
        if state and state.get("price"):
            return float(state["price"])
    except Exception:
        pass
    return None


def mark_recommendation(rec: dict, is_market_open: bool) -> dict:
    """
    Calculate current value + P&L for a single recommendation.

    P&L percentage is always computed against CAPITAL AT RISK, not
    premium flow direction:
      Debit trades:  capital at risk = entry_debit (premium paid) — unchanged
      Credit trades: capital at risk = max_loss (NOT credit received —
                     that was the bug). max_loss is stored per-contract
                     in the DB (already ×100 vs the per-share entry_debit),
                     so it's converted to a per-share basis before the ratio.

    Also returns pct_of_max_profit_captured — "how much of the
    theoretical max profit have I realized" — which is the metric that
    actually matches Trading Rule 1 ("close at 80% of max profit"). This
    is informational for the UI, separate from pnl_pct which feeds
    backtest stats and must be comparable across debit and credit trades.
    """
    ticker      = rec.get("ticker", "")
    legs        = rec.get("legs") or []
    entry_debit = float(rec.get("entry_debit", 0) or 0)
    entry_low   = float(rec.get("entry_zone_low", 0) or 0)
    max_loss    = float(rec.get("max_loss", 0) or 0)     # per-contract, from DB
    max_profit  = float(rec.get("max_profit", 0) or 0)   # per-contract, from DB

    result = {
        "current_value": None,
        "pnl_dollars":   None,
        "pnl_pct":       None,
        "pct_of_max_profit_captured": None,
        "mark_type":     "live" if is_market_open else "eod_close",
    }

    if legs:
        current_value = get_current_option_value(ticker, legs)
        if current_value is not None and entry_debit:
            result["current_value"] = current_value

            if entry_debit > 0:
                # Debit spread: paid entry_debit, profit when value rises.
                # Capital at risk = entry_debit — already correct.
                pnl_per_share        = current_value - entry_debit
                risk_basis_per_share = entry_debit
            else:
                # Credit spread / iron condor: received |entry_debit|,
                # profit when cost to close falls. Capital at risk is
                # max_loss (the actual dollar exposure), NOT the credit
                # received — dividing by credit inflated returns by
                # roughly (max_loss / credit), ~3x in tested cases.
                pnl_per_share = abs(entry_debit) - current_value
                risk_basis_per_share = (max_loss / 100.0) if max_loss > 0 else abs(entry_debit)

            pnl_dollars = round(pnl_per_share * 100, 2)
            result["pnl_dollars"] = pnl_dollars
            result["pnl_pct"] = (
                round((pnl_per_share / risk_basis_per_share) * 100, 1)
                if risk_basis_per_share else None
            )
            if max_profit > 0:
                result["pct_of_max_profit_captured"] = round(pnl_dollars / max_profit * 100, 1)
    else:
        current_price = get_current_stock_value(ticker)
        if current_price is not None and entry_low:
            result["current_value"] = current_price
            pnl_per_share = current_price - entry_low
            result["pnl_dollars"] = round(pnl_per_share, 2)
            result["pnl_pct"] = round((pnl_per_share / entry_low) * 100, 1) if entry_low else None

    return result


def mark_all_active_recommendations(user_id: str, days_back: int = 90) -> dict:
    """
    Mark all recent recommendations to market.
    Called lazily when History tab loads and existing marks are stale.
    """
    from sqlalchemy import text
    from app.db.session import get_session
    from app.scanner.quick_scan import _is_market_open

    is_open      = _is_market_open()
    marked_count = 0
    error_count  = 0

    try:
        with get_session() as s:
            rows = s.execute(text("""
                SELECT id, ticker, legs, entry_debit, entry_zone_low, max_loss, max_profit
                FROM daily_recommendations
                WHERE user_id = :uid
                  AND date >= CURRENT_DATE - :days
                  AND status != 'INVALIDATED'
            """), {"uid": user_id, "days": days_back}).fetchall()

        for row in rows:
            rec = {
                "ticker":          row.ticker,
                "legs":            row.legs or [],
                "entry_debit":     float(row.entry_debit or 0),
                "entry_zone_low":  float(row.entry_zone_low or 0),
                "max_loss":        float(row.max_loss or 0),
                "max_profit":      float(row.max_profit or 0),
            }
            mark = mark_recommendation(rec, is_open)

            if mark["current_value"] is not None:
                try:
                    with get_session() as s:
                        s.execute(text("""
                            UPDATE daily_recommendations
                            SET current_value = :cv,
                                current_pnl_dollars = :pnld,
                                current_pnl_pct = :pnlp,
                                last_marked_at = now(),
                                mark_type = :mt
                            WHERE id = :id
                        """), {
                            "cv": mark["current_value"], "pnld": mark["pnl_dollars"],
                            "pnlp": mark["pnl_pct"], "mt": mark["mark_type"], "id": row.id,
                        })
                    marked_count += 1
                except Exception as e:
                    print(f"[MarkToMarket] DB update failed for {row.ticker}: {e}")
                    error_count += 1
            else:
                error_count += 1

        print(f"[MarkToMarket] Marked {marked_count}/{len(rows)} recs "
              f"(market {'open' if is_open else 'closed'})")
        return {"marked": marked_count, "errors": error_count,
                "total": len(rows), "market_open": is_open}

    except Exception as e:
        print(f"[MarkToMarket] Job failed: {e}")
        return {"marked": 0, "errors": 1, "total": 0, "error": str(e)}


def calculate_backtest_stats(user_id: str, days_back: int = 90) -> dict:
    """
    Aggregate stats from marked recommendations — feeds learning/calibration.
    Answers: is conviction scoring actually predictive? Which strategies win?

    Excludes rows flagged excluded_from_stats (corrupted historical P&L
    from a fixed upstream bug — see the July 2026 sanity-cap rewrite of
    app/strategy/engine.py) so a handful of phantom $1M+ "max profit"
    trades don't dominate the averages.
    """
    from sqlalchemy import text
    from app.db.session import get_session

    with get_session() as s:
        rows = s.execute(text("""
            SELECT ticker, direction, strategy, horizon, conviction_score,
                   conviction_tier, current_pnl_pct, current_pnl_dollars, date
            FROM daily_recommendations
            WHERE user_id = :uid AND date >= CURRENT_DATE - :days
              AND current_pnl_pct IS NOT NULL
              AND (excluded_from_stats IS NULL OR excluded_from_stats = FALSE)
        """), {"uid": user_id, "days": days_back}).fetchall()

    if not rows:
        return {"available": False, "reason": "No marked recommendations yet"}

    def _agg(rows, key_fn):
        groups: dict = {}
        for r in rows:
            k = key_fn(r) or "UNKNOWN"
            groups.setdefault(k, []).append(float(r.current_pnl_pct))
        return {
            k: {
                "count":       len(pnls),
                "win_rate":    round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
                "avg_pnl_pct": round(sum(pnls) / len(pnls), 1),
            }
            for k, pnls in groups.items()
        }

    tier_stats     = _agg(rows, lambda r: r.conviction_tier)
    strategy_stats = _agg(rows, lambda r: r.strategy)
    horizon_stats  = _agg(rows, lambda r: r.horizon)

    all_pnls = [float(r.current_pnl_pct) for r in rows]
    overall_win_rate = round(sum(1 for p in all_pnls if p > 0) / len(all_pnls) * 100, 1)
    overall_avg_pnl  = round(sum(all_pnls) / len(all_pnls), 1)

    insights = []
    if "HIGH" in tier_stats and "MODERATE" in tier_stats:
        hi, mo = tier_stats["HIGH"]["win_rate"], tier_stats["MODERATE"]["win_rate"]
        if hi > mo + 10:
            insights.append(f"Conviction scoring well-calibrated: HIGH wins {hi}% vs MODERATE {mo}%")
        elif hi < mo:
            insights.append(f"⚠️ HIGH tier ({hi}%) underperforming MODERATE ({mo}%) — scoring may need review")
    if strategy_stats:
        best = max(strategy_stats.items(), key=lambda x: x[1]["win_rate"])
        insights.append(f"Best strategy: {best[0]} ({best[1]['win_rate']}% win, {best[1]['count']} trades)")

    return {
        "available":             True,
        "total_recommendations": len(rows),
        "overall_win_rate":      overall_win_rate,
        "overall_avg_pnl_pct":   overall_avg_pnl,
        "by_conviction_tier":    tier_stats,
        "by_strategy":           strategy_stats,
        "by_horizon":            horizon_stats,
        "insight":               " | ".join(insights) if insights else "Gathering more data for reliable insights",
    }
