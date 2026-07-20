"""
C11 Learning Engine (W16).

Analyses all historical data to generate actionable insights and
adjust strategy confidence weights for future recommendations.

Learning sources:
    sell_recommendations   — did user act? was signal correct?
    daily_recommendations  — win/loss per strategy, direction, ticker
    position_alerts        — what patterns in ignored alerts?
    user_profiles          — baseline risk profile

Outputs:
    1. Behavioral insights  — what does the user tend to ignore?
    2. Strategy performance — win rate per strategy, best tickers
    3. Weight adjustments   — tune strategy engine confidence
    4. Action items         — specific things to do right now

Auto-wired: called after every log_outcome() to keep learnings current.
"""
from datetime import datetime, timedelta
from collections import defaultdict


# ─────────────────────────────────────────────────────────────────────────────
# Sell Signal Compliance Analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyze_sell_signal_compliance(user_id: str) -> dict:
    """
    Did the user act on our sell signals?
    Finds patterns in what gets ignored and at what cost.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        with get_session() as s:
            rows = s.execute(text("""
                SELECT symbol, pnl_pct, llm_action, llm_confidence,
                       user_acted, was_correct, outcome_pnl,
                       recommended_at,
                       -- How many times did we recommend selling same symbol?
                       COUNT(*) OVER (PARTITION BY symbol) AS signal_count
                FROM sell_recommendations
                WHERE user_id = :uid
                ORDER BY recommended_at DESC
            """), {"uid": user_id}).fetchall()

        if not rows:
            return {
                "total_signals": 0,
                "message": "No sell signals logged yet. Signals are logged automatically when you run get_sell_signals()."
            }

        total     = len(rows)
        # NULL = never told us, FALSE = explicitly ignored — both count as not acted
        acted     = [r for r in rows if r.user_acted is True]
        ignored   = [r for r in rows if not r.user_acted]  # NULL or FALSE
        stops     = [r for r in rows if r.llm_action == "FULL_EXIT"]
        repeated  = [r for r in rows if r.signal_count > 1]

        # Cost of ignoring — estimate based on P&L at time of signal
        ignored_stops = [r for r in ignored if r.llm_action == "FULL_EXIT"]
        est_saved = 0.0
        for r in ignored_stops:
            # If we said exit at -40% and they didn't, loss got worse
            # Conservative estimate: P&L dropped another 10% on average
            est_saved += abs(float(r.pnl_pct or 0)) * 0.1

        # Which symbols are repeatedly ignored?
        ignored_by_symbol: dict = defaultdict(list)
        for r in ignored:
            ignored_by_symbol[r.symbol].append(float(r.pnl_pct or 0))

        chronic_ignores = {
            sym: {
                "times_ignored": len(pnls),
                "latest_pnl":    round(pnls[0], 1) if pnls else None,
                "trend":         "getting worse" if len(pnls) > 1 and pnls[0] < pnls[-1]
                                 else "stable"
            }
            for sym, pnls in ignored_by_symbol.items()
            if len(pnls) >= 1
        }

        return {
            "total_signals":       total,
            "acted_on":            len(acted),
            "ignored":             len(ignored),
            "compliance_rate":     round(len(acted) / total * 100, 1),
            "stop_loss_signals":   len(stops),
            "repeated_signals":    len(set(r.symbol for r in repeated)),
            "chronic_ignores":     chronic_ignores,
            "insight": _sell_compliance_insight(len(acted), total, chronic_ignores),
        }

    except Exception as e:
        return {"error": str(e)}


def _sell_compliance_insight(acted: int, total: int, chronic: dict) -> str:
    if total == 0:
        return "No sell signals yet"
    rate = acted / total * 100
    parts = []
    if rate < 30:
        parts.append(f"You act on only {rate:.0f}% of sell signals — consider reviewing them more promptly")
    elif rate < 70:
        parts.append(f"You act on {rate:.0f}% of sell signals — room for improvement")
    else:
        parts.append(f"Good compliance — acting on {rate:.0f}% of sell signals")

    if chronic:
        worst = max(chronic, key=lambda k: chronic[k]["times_ignored"])
        n = chronic[worst]["times_ignored"]
        pnl = chronic[worst]["latest_pnl"]
        parts.append(
            f"{worst} has been recommended for exit {n} time(s) and ignored each time "
            f"(currently {pnl:+.1f}%)"
        )
    return ". ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy Performance Analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyze_strategy_performance(user_id: str) -> dict:
    """
    Win rate per strategy, direction, ticker from closed trades.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        with get_session() as s:
            # ticker AS symbol — keeps every downstream r.symbol reference
            # unchanged. All these columns now live on daily_recommendations
            # (Step 1 + prediction_tracker.log_exit() from Step 2).
            rows = s.execute(text("""
                SELECT ticker AS symbol, strategy, direction,
                       actual_pnl_pct, risk_reward,
                       was_correct, user_executed,
                       executed_at, closed_at
                FROM daily_recommendations
                WHERE user_id   = :uid
                  AND was_correct IS NOT NULL
                  AND (excluded_from_stats IS NULL OR excluded_from_stats = FALSE)
            """), {"uid": user_id}).fetchall()

        if not rows:
            return {
                "total_closed": 0,
                "message": "No closed trades yet. Execute a recommendation and log the outcome to start learning."
            }

        # By strategy
        by_strat: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "pnls": []})
        for r in rows:
            key = r.strategy or "UNKNOWN"
            by_strat[key]["wins"   if r.was_correct else "losses"] += 1
            if r.actual_pnl_pct:
                by_strat[key]["pnls"].append(float(r.actual_pnl_pct))

        strat_stats = {}
        for strat, data in by_strat.items():
            total  = data["wins"] + data["losses"]
            wr     = round(data["wins"] / total * 100, 1)
            avg_pnl = round(sum(data["pnls"]) / len(data["pnls"]), 1) if data["pnls"] else None
            strat_stats[strat] = {
                "wins": data["wins"], "losses": data["losses"],
                "win_rate": wr, "avg_pnl_pct": avg_pnl,
            }

        # By ticker
        by_ticker: dict = defaultdict(lambda: {"wins": 0, "losses": 0})
        for r in rows:
            by_ticker[r.symbol]["wins" if r.was_correct else "losses"] += 1

        ticker_stats = {
            sym: {
                "wins": d["wins"], "losses": d["losses"],
                "win_rate": round(d["wins"] / (d["wins"] + d["losses"]) * 100, 1)
            }
            for sym, d in by_ticker.items()
        }

        # By direction
        by_dir: dict = defaultdict(lambda: {"wins": 0, "losses": 0})
        for r in rows:
            by_dir[r.direction or "UNKNOWN"]["wins" if r.was_correct else "losses"] += 1

        dir_stats = {
            d: {"wins": v["wins"], "losses": v["losses"],
                "win_rate": round(v["wins"] / (v["wins"] + v["losses"]) * 100, 1)}
            for d, v in by_dir.items()
        }

        total_wins = sum(1 for r in rows if r.was_correct)
        overall_wr = round(total_wins / len(rows) * 100, 1)

        best_strat  = max(strat_stats, key=lambda k: strat_stats[k]["win_rate"], default=None)
        worst_strat = min(strat_stats, key=lambda k: strat_stats[k]["win_rate"], default=None)
        best_ticker = max(ticker_stats, key=lambda k: ticker_stats[k]["win_rate"], default=None)

        return {
            "total_closed":    len(rows),
            "overall_win_rate": overall_wr,
            "total_wins":      total_wins,
            "total_losses":    len(rows) - total_wins,
            "by_strategy":     strat_stats,
            "by_ticker":       ticker_stats,
            "by_direction":    dir_stats,
            "best_strategy":   best_strat,
            "worst_strategy":  worst_strat,
            "best_ticker":     best_ticker,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Strategy Weight Adjustments
# ─────────────────────────────────────────────────────────────────────────────

# Baseline confidence weights (0.5 = neutral, >0.5 = boost, <0.5 = reduce)
DEFAULT_WEIGHTS = {
    "DEBIT_PUT_SPREAD":   0.5,
    "DEBIT_CALL_SPREAD":  0.5,
    "CREDIT_CALL_SPREAD": 0.5,
    "CREDIT_PUT_SPREAD":  0.5,
    "IRON_CONDOR":        0.5,
    "LONG_STRADDLE":      0.5,
    "LONG_PUT":           0.5,
    "LONG_CALL":          0.5,
}


def calculate_weight_adjustments(user_id: str) -> dict[str, float]:
    """
    Calculate confidence weight adjustments based on win rates.
    Win rate > 60% → boost weight → strategy engine gives higher confidence
    Win rate < 40% → reduce weight → strategy engine penalises this strategy

    Returns {strategy: adjustment_factor} where:
        1.0 = no change
        1.2 = 20% confidence boost
        0.8 = 20% confidence reduction
    """
    perf = analyze_strategy_performance(user_id)
    if perf.get("total_closed", 0) < 3:
        # Not enough data — use defaults
        return {s: 1.0 for s in DEFAULT_WEIGHTS}

    by_strat = perf.get("by_strategy", {})
    adjustments = {}

    for strat in DEFAULT_WEIGHTS:
        if strat not in by_strat:
            adjustments[strat] = 1.0
            continue

        wr    = by_strat[strat]["win_rate"]
        total = by_strat[strat]["wins"] + by_strat[strat]["losses"]

        # Only adjust if we have meaningful sample size
        if total < 3:
            adjustments[strat] = 1.0
        elif wr >= 70:
            adjustments[strat] = 1.3    # strong boost
        elif wr >= 60:
            adjustments[strat] = 1.15   # moderate boost
        elif wr <= 30:
            adjustments[strat] = 0.7    # strong penalty
        elif wr <= 40:
            adjustments[strat] = 0.85   # moderate penalty
        else:
            adjustments[strat] = 1.0    # neutral

    return adjustments


def get_strategy_weights(user_id: str) -> dict:
    """
    Get current strategy weight adjustments with explanation.
    These are applied in strategy engine to tune confidence scores.
    """
    adjustments = calculate_weight_adjustments(user_id)
    perf        = analyze_strategy_performance(user_id)

    explained = {}
    for strat, factor in adjustments.items():
        by_s = perf.get("by_strategy", {}).get(strat)
        if by_s and factor != 1.0:
            wr = by_s["win_rate"]
            explained[strat] = {
                "factor":   factor,
                "win_rate": wr,
                "effect":   "boost" if factor > 1.0 else "reduce",
                "reason":   f"{wr:.0f}% win rate over {by_s['wins']+by_s['losses']} trades",
            }
        elif factor == 1.0:
            explained[strat] = {
                "factor":  1.0,
                "effect":  "neutral",
                "reason":  "insufficient data or at baseline",
            }

    return {
        "adjustments":  adjustments,
        "explained":    explained,
        "data_basis":   perf.get("total_closed", 0),
        "note":         "Applied automatically in get_strategy_recommendation()"
                        if perf.get("total_closed", 0) >= 3
                        else "Need 3+ closed trades for adjustments to activate",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Update User Profile
# ─────────────────────────────────────────────────────────────────────────────

def update_user_profile(user_id: str) -> dict:
    """
    Sync user_profiles table with latest learnings.
    Called after every log_outcome().
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        perf        = analyze_strategy_performance(user_id)
        compliance  = analyze_sell_signal_compliance(user_id)

        best  = perf.get("best_strategy")
        worst = perf.get("worst_strategy")
        wr    = perf.get("overall_win_rate")

        # Infer risk tolerance from behavior
        compliance_rate = compliance.get("compliance_rate", 50)
        if compliance_rate < 30:
            risk_tolerance = "high"     # ignores stop losses
        elif compliance_rate > 70:
            risk_tolerance = "low"      # acts on signals quickly
        else:
            risk_tolerance = "moderate"

        with get_session() as s:
            s.execute(text("""
                UPDATE user_profiles
                SET best_performing_strategy  = :best,
                    worst_performing_strategy = :worst,
                    risk_tolerance            = :risk,
                    updated_at                = now()
                WHERE user_id = :uid
            """), {
                "uid": user_id, "best": best,
                "worst": worst, "risk": risk_tolerance,
            })

        return {
            "updated":              True,
            "best_strategy":        best,
            "worst_strategy":       worst,
            "inferred_risk_profile": risk_tolerance,
            "win_rate":             wr,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Full Learning Report
# ─────────────────────────────────────────────────────────────────────────────

def get_learning_report(user_id: str) -> dict:
    """
    Complete learning report combining all analyses.
    Generates prioritised action items and insights.
    """
    compliance = analyze_sell_signal_compliance(user_id)
    perf       = analyze_strategy_performance(user_id)
    weights    = get_strategy_weights(user_id)

    # Generate action items
    actions = []

    # From sell compliance
    chronic = compliance.get("chronic_ignores", {})
    for sym, data in chronic.items():
        n   = data["times_ignored"]
        pnl = data["latest_pnl"]
        if pnl and pnl < -30:
            actions.append({
                "priority": "HIGH",
                "action":   f"Exit {sym}",
                "reason":   f"Recommended exit {n}x, currently at {pnl:+.1f}% — losses compounding",
            })

    # From strategy performance
    if perf.get("worst_strategy") and perf.get("total_closed", 0) >= 3:
        ws = perf["worst_strategy"]
        wr = perf["by_strategy"].get(ws, {}).get("win_rate", 0)
        if wr < 40:
            actions.append({
                "priority": "MEDIUM",
                "action":   f"Avoid {ws}",
                "reason":   f"Only {wr:.0f}% win rate — weakest performing strategy",
            })

    if perf.get("best_strategy") and perf.get("total_closed", 0) >= 3:
        bs = perf["best_strategy"]
        wr = perf["by_strategy"].get(bs, {}).get("win_rate", 0)
        if wr >= 60:
            actions.append({
                "priority": "LOW",
                "action":   f"Prefer {bs}",
                "reason":   f"{wr:.0f}% win rate — strongest performing strategy",
            })

    # Generate insights
    insights = []
    if compliance.get("total_signals", 0) > 0:
        insights.append(compliance.get("insight", ""))
    if perf.get("total_closed", 0) > 0:
        wr = perf.get("overall_win_rate", 0)
        insights.append(
            f"Overall win rate: {wr}% across {perf['total_closed']} closed trades"
        )
    if perf.get("best_ticker"):
        bt = perf["best_ticker"]
        btr = perf["by_ticker"].get(bt, {})
        insights.append(
            f"Best ticker: {bt} — {btr.get('win_rate', 0)}% win rate "
            f"({btr.get('wins', 0)}W/{btr.get('losses', 0)}L)"
        )

    # Format for display
    return {
        "generated_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sell_compliance":  compliance,
        "strategy_performance": perf,
        "weight_adjustments":   weights,
        "action_items":         sorted(actions,
                                       key=lambda x: {"HIGH":0,"MEDIUM":1,"LOW":2}.get(x["priority"],3)),
        "insights":             [i for i in insights if i],
        "summary":              _generate_summary(compliance, perf, actions),
    }


def _generate_summary(compliance: dict, perf: dict, actions: list) -> str:
    parts = []

    # Sell compliance summary
    cr = compliance.get("compliance_rate", 0)
    ts = compliance.get("total_signals", 0)
    if ts > 0:
        parts.append(f"Acting on {cr:.0f}% of {ts} sell signals")

    # Strategy performance
    tc = perf.get("total_closed", 0)
    wr = perf.get("overall_win_rate", 0)
    if tc > 0:
        parts.append(f"{wr}% win rate across {tc} closed trades")
    else:
        parts.append("No closed trades yet — execute a recommendation to start learning")

    # Urgent actions
    high = [a for a in actions if a["priority"] == "HIGH"]
    if high:
        parts.append(f"{len(high)} urgent action(s) require attention")

    return " | ".join(parts) if parts else "Learning engine ready — start trading to generate insights"