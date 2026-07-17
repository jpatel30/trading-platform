"""
Phase C1 — Backtester.

Three backtests using available data:

Backtest 1: "Cost of ignoring sell signals" (ACTIVE — has data)
    Compare P&L at first sell signal vs current P&L
    "If you had exited GLD at -17% (first signal), you avoided -77% more loss"
    Dollar amounts calculated from cost_basis in sell_recommendations

Backtest 2: "Entry quality" (FRAMEWORK — needs daily_rec outcomes)
    Did AT_RESISTANCE entries outperform BETWEEN_LEVELS?
    Did volume-confirmed entries win more?
    Activates after 10+ closed daily_recommendations (user_executed trades)

Backtest 3: "Conviction gate" (FRAMEWORK — needs daily_rec outcomes)
    Did conviction ≥70 picks outperform <70?
    Which conviction criteria were most predictive?
    Activates after 10+ closed daily_recommendations
"""
from datetime import datetime, date


# ─────────────────────────────────────────────────────────────────────────────
# Backtest 1: Cost of Ignoring Sell Signals
# ─────────────────────────────────────────────────────────────────────────────

def backtest_sell_signal_cost(user_id: str) -> dict:
    """
    For each symbol, compare:
      - P&L when first sell signal fired
      - P&L when most recent sell signal fired (or current)

    Cost of delay = additional loss/missed gain from not acting on first signal.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        with get_session() as s:
            # Get first and latest signal per symbol
            rows = s.execute(text("""
                SELECT
                    symbol,
                    MIN(pnl_pct)                                      AS min_pnl,
                    MAX(pnl_pct)                                       AS max_pnl,
                    COUNT(*)                                           AS signal_count,
                    MIN(pnl_pct) FILTER (WHERE llm_action IS NOT NULL) AS first_signal_pnl,
                    MAX(ABS(pnl_pct))                                  AS worst_pnl,
                    MAX(recommended_at)                                AS latest_signal_at,
                    MIN(recommended_at)                                AS first_signal_at,
                    MAX(cost_basis)                                    AS cost_basis,
                    BOOL_OR(user_acted)                                AS ever_acted
                FROM sell_recommendations
                WHERE user_id = :uid
                GROUP BY symbol
                ORDER BY MIN(pnl_pct)
            """), {"uid": user_id}).fetchall()

        if not rows:
            return {
                "available":    False,
                "message":      "No sell recommendations found. Run get_sell_signals() to generate data.",
            }

        results     = []
        total_saved = 0.0
        total_cost  = 0.0

        for r in rows:
            symbol      = r.symbol
            first_pnl   = float(r.first_signal_pnl or r.min_pnl or 0)
            latest_pnl  = float(r.worst_pnl or r.max_pnl or first_pnl)
            cost_basis  = float(r.cost_basis or 0)
            signals     = int(r.signal_count)
            acted       = bool(r.ever_acted)

            # For losers: delay cost = additional % loss after first signal
            # For winners: delay cost = % gain given up by exiting (could be negative = good hold)
            is_loser      = first_pnl < 0
            delay_pct     = latest_pnl - first_pnl  # negative = got worse

            # Dollar impact (if cost_basis available)
            if cost_basis > 0:
                dollar_at_first  = cost_basis * (first_pnl / 100)
                dollar_at_latest = cost_basis * (-latest_pnl / 100) if is_loser else cost_basis * (latest_pnl / 100)
                delay_dollars    = cost_basis * (abs(delay_pct) / 100)
            else:
                dollar_at_first  = 0
                dollar_at_latest = 0
                delay_dollars    = 0

            if is_loser:
                # If delay made it worse (more negative)
                if delay_pct < -1:
                    outcome = "GOT_WORSE"
                    action  = f"Exiting at {first_pnl:.1f}% would have saved ${delay_dollars:,.0f} in additional losses"
                    total_saved += delay_dollars
                elif delay_pct > 1:
                    outcome = "RECOVERED"
                    action  = f"Position recovered {delay_pct:.1f}% after signal — hold was correct"
                else:
                    outcome = "STABLE"
                    action  = "Position stayed flat after signal"
            else:
                # Winner — did holding capture more gains or did it reverse?
                if delay_pct < -5:
                    outcome = "GAVE_BACK_GAINS"
                    action  = f"Held past signal — gave back {abs(delay_pct):.1f}% of gains"
                    total_cost += delay_dollars
                elif delay_pct > 5:
                    outcome = "MORE_UPSIDE"
                    action  = f"Held past signal — captured {delay_pct:.1f}% more gains"
                else:
                    outcome = "STABLE"
                    action  = "Position held its gains after signal"

            results.append({
                "symbol":           symbol,
                "first_signal_pnl": round(first_pnl, 1),
                "latest_pnl":       round(-latest_pnl if is_loser else latest_pnl, 1),
                "delay_pct":        round(delay_pct, 1),
                "delay_dollars":    round(delay_dollars, 0),
                "signal_count":     signals,
                "acted":            acted,
                "outcome":          outcome,
                "action_insight":   action,
                "cost_basis":       round(cost_basis, 0),
                "is_loser":         is_loser,
            })

        # Summary
        ignored_losers  = [r for r in results if r["is_loser"] and not r["acted"] and r["outcome"] == "GOT_WORSE"]
        ignored_winners = [r for r in results if not r["is_loser"] and not r["acted"] and r["outcome"] == "GAVE_BACK_GAINS"]

        return {
            "available":          True,
            "backtest":           "sell_signal_cost",
            "results":            results,
            "summary": {
                "total_symbols":       len(results),
                "ignored_losers":      len(ignored_losers),
                "ignored_winners":     len(ignored_winners),
                "total_additional_loss_from_ignoring": round(total_saved, 0),
                "total_gains_given_back":              round(total_cost, 0),
                "net_cost_of_ignoring":                round(total_saved + total_cost, 0),
                "insight": _sell_backtest_insight(ignored_losers, ignored_winners, total_saved, total_cost),
            },
        }

    except Exception as e:
        return {"available": False, "error": str(e)}


def _sell_backtest_insight(losers, winners, saved, gave_back) -> str:
    parts = []
    if losers:
        syms = ", ".join(r["symbol"] for r in losers[:3])
        parts.append(
            f"Ignoring sell signals on {syms} cost an additional "
            f"${saved:,.0f} in losses beyond the first signal"
        )
    if winners:
        syms = ", ".join(r["symbol"] for r in winners[:2])
        parts.append(
            f"Holding {syms} past profit signals gave back ${gave_back:,.0f} in gains"
        )
    if not parts:
        parts.append("No significant cost from signal compliance yet — keep running signals")
    return ". ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Backtest 2: Entry Quality (needs strategy_rec outcomes)
# ─────────────────────────────────────────────────────────────────────────────

def backtest_entry_quality(user_id: str) -> dict:
    """
    Did AT_RESISTANCE/AT_SUPPORT entries outperform BETWEEN_LEVELS?
    Did volume-confirmed entries win more?
    Needs: closed strategy_recommendations with entry_trigger data.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        with get_session() as s:
            # actual_pnl_pct/was_correct now live directly on
            # daily_recommendations (Step 1 + prediction_tracker.log_exit())
            # — no JOIN needed, and this sees every real close going forward.
            rows = s.execute(text("""
                SELECT entry_trigger, conviction_score,
                       actual_pnl_pct, was_correct
                FROM daily_recommendations
                WHERE user_id = :uid
                  AND was_correct IS NOT NULL
                  AND actual_pnl_pct IS NOT NULL
            """), {"uid": user_id}).fetchall()

        if len(rows) < 5:
            return {
                "available":  False,
                "message":    f"Need 5+ closed trades for entry quality analysis. Have {len(rows)}.",
                "backtest":   "entry_quality",
                "data_count": len(rows),
            }

        # Group by entry trigger
        from collections import defaultdict
        by_trigger: dict = defaultdict(lambda: {"wins": 0, "total": 0, "pnls": []})
        for r in rows:
            trigger = r.entry_trigger or "UNKNOWN"
            by_trigger[trigger]["total"] += 1
            if r.was_correct:
                by_trigger[trigger]["wins"] += 1
            if r.actual_pnl_pct:
                by_trigger[trigger]["pnls"].append(float(r.actual_pnl_pct))

        trigger_stats = {
            t: {
                "win_rate": round(d["wins"]/d["total"]*100, 1),
                "avg_pnl":  round(sum(d["pnls"])/len(d["pnls"]), 1) if d["pnls"] else None,
                "trades":   d["total"],
            }
            for t, d in by_trigger.items()
        }

        best = max(trigger_stats, key=lambda k: trigger_stats[k]["win_rate"], default=None)

        return {
            "available":     True,
            "backtest":      "entry_quality",
            "by_trigger":    trigger_stats,
            "best_trigger":  best,
            "data_count":    len(rows),
            "insight": (
                f"Best entry trigger: {best} with {trigger_stats[best]['win_rate']}% win rate"
                if best else "Insufficient data"
            ),
        }

    except Exception as e:
        return {"available": False, "error": str(e), "backtest": "entry_quality"}


# ─────────────────────────────────────────────────────────────────────────────
# Backtest 3: Conviction Gate
# ─────────────────────────────────────────────────────────────────────────────

def backtest_conviction_gate(user_id: str) -> dict:
    """
    Did conviction ≥70 picks outperform <70?
    Which conviction criteria were most predictive?
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        with get_session() as s:
            # Same fix as backtest_entry_quality() above — single table now.
            rows = s.execute(text("""
                SELECT conviction_score, conviction_tier,
                       conviction_breakdown,
                       actual_pnl_pct, was_correct
                FROM daily_recommendations
                WHERE user_id = :uid
                  AND was_correct IS NOT NULL
            """), {"uid": user_id}).fetchall()

        if len(rows) < 10:
            return {
                "available":  False,
                "message":    f"Need 10+ closed trades for conviction analysis. Have {len(rows)}.",
                "backtest":   "conviction_gate",
                "data_count": len(rows),
            }

        import json
        high_conv  = [r for r in rows if (r.conviction_score or 0) >= 70]
        low_conv   = [r for r in rows if (r.conviction_score or 0) < 70]

        high_wr = round(sum(1 for r in high_conv if r.was_correct) / max(len(high_conv),1) * 100, 1)
        low_wr  = round(sum(1 for r in low_conv  if r.was_correct) / max(len(low_conv), 1) * 100, 1)

        # Per-criterion analysis
        from collections import defaultdict
        criterion_results: dict = defaultdict(lambda: {"wins": 0, "total": 0})
        for r in rows:
            if not r.conviction_breakdown:
                continue
            breakdown = json.loads(r.conviction_breakdown) if isinstance(r.conviction_breakdown, str) else r.conviction_breakdown
            for crit, data in breakdown.items():
                pts = data.get("points", 0)
                weight = {"entry_trigger":25,"volume":20,"iv_rank":15,"options_flow":20,"vix_zone":10,"ta_alignment":10}.get(crit,10)
                passed = pts >= weight * 0.7
                criterion_results[crit]["total"] += 1
                if r.was_correct and passed:
                    criterion_results[crit]["wins"] += 1

        crit_accuracy = {
            k: round(v["wins"]/max(v["total"],1)*100, 1)
            for k, v in criterion_results.items()
        }
        best_crit = max(crit_accuracy, key=crit_accuracy.get, default=None)

        return {
            "available":        True,
            "backtest":         "conviction_gate",
            "high_conviction":  {"threshold": "≥70", "trades": len(high_conv), "win_rate": high_wr},
            "low_conviction":   {"threshold": "<70",  "trades": len(low_conv),  "win_rate": low_wr},
            "gate_effectiveness": round(high_wr - low_wr, 1),
            "best_criterion":   best_crit,
            "criterion_accuracy": crit_accuracy,
            "data_count":       len(rows),
            "insight": (
                f"Conviction ≥70 wins {high_wr}% vs {low_wr}% for <70 "
                f"(+{high_wr-low_wr:.1f}% improvement from gate). "
                f"Most predictive criterion: {best_crit}"
                if len(rows) >= 10 else "Insufficient data"
            ),
        }

    except Exception as e:
        return {"available": False, "error": str(e), "backtest": "conviction_gate"}


# ─────────────────────────────────────────────────────────────────────────────
# Full Backtest Report
# ─────────────────────────────────────────────────────────────────────────────

def run_full_backtest(user_id: str) -> dict:
    """Run all three backtests and combine into one report."""
    bt1 = backtest_sell_signal_cost(user_id)
    bt2 = backtest_entry_quality(user_id)
    bt3 = backtest_conviction_gate(user_id)

    return {
        "generated_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sell_signal_cost":  bt1,
        "entry_quality":     bt2,
        "conviction_gate":   bt3,
        "formatted":         format_backtest_report(bt1, bt2, bt3),
    }


def format_backtest_report(bt1: dict, bt2: dict, bt3: dict) -> str:
    lines = ["## Backtest Report", ""]

    # Backtest 1
    lines.append("### 1. Cost of Ignoring Sell Signals")
    if bt1.get("available"):
        s = bt1["summary"]
        lines.append(f"**Net cost of ignoring signals: ${s['net_cost_of_ignoring']:,.0f}**")
        lines.append(f"Additional losses from ignored stop signals: ${s['total_additional_loss_from_ignoring']:,.0f}")
        lines.append(f"Insight: {s['insight']}")
        lines.append("")
        lines.append("Per position:")
        for r in bt1["results"]:
            icon = "🔴" if r["outcome"] == "GOT_WORSE" else "🟢" if r["outcome"] in ("RECOVERED","MORE_UPSIDE") else "⚪"
            lines.append(
                f"  {icon} **{r['symbol']}** — first signal {r['first_signal_pnl']:+.1f}% "
                f"→ latest {r['latest_pnl']:+.1f}% | {r['action_insight']}"
            )
    else:
        lines.append(f"⚠️ {bt1.get('message', 'Not available')}")
    lines.append("")

    # Backtest 2
    lines.append("### 2. Entry Quality")
    if bt2.get("available"):
        lines.append(f"Best entry trigger: **{bt2['best_trigger']}**")
        for trigger, stats in bt2["by_trigger"].items():
            lines.append(f"  {trigger}: {stats['win_rate']}% win rate ({stats['trades']} trades)")
        lines.append(f"Insight: {bt2['insight']}")
    else:
        lines.append(f"⏳ {bt2.get('message', 'Not available')} — framework ready")
    lines.append("")

    # Backtest 3
    lines.append("### 3. Conviction Gate")
    if bt3.get("available"):
        lines.append(f"Score ≥70: **{bt3['high_conviction']['win_rate']}%** win rate")
        lines.append(f"Score <70: **{bt3['low_conviction']['win_rate']}%** win rate")
        lines.append(f"Gate improvement: **+{bt3['gate_effectiveness']}%**")
        lines.append(f"Insight: {bt3['insight']}")
    else:
        lines.append(f"⏳ {bt3.get('message', 'Not available')} — framework ready")

    return "\n".join(lines)
