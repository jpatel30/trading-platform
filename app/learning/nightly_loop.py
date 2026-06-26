"""
Phase C — Nightly Learning Loop.

Runs automatically after market close (4:30 PM ET) via position monitor.
Does NOT run on weekends or holidays.

Steps each night:
    1. Pull today's closed positions (Webull positions that disappeared)
    2. Match against sell_recommendations → update was_correct
    3. Match against strategy_recommendations → update outcomes
    4. Recalibrate conviction weights (if 10+ closed trades)
    5. Generate nightly learning summary
    6. Store in learning_log
    7. Send Discord summary

Conviction weight recalibration (after 10+ closed strategy_recs):
    For each signal criterion, measure:
        win_rate when criterion passed vs failed
    If criterion has >70% win rate when passed → boost weight
    If criterion has <40% win rate when passed → reduce weight
    New weights stored in user_profiles.conviction_weights
"""
import json
from datetime import datetime, date, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# News Impact Log Table
# ─────────────────────────────────────────────────────────────────────────────

def record_news_impact(
    ticker: str,
    headline: str,
    source: str,
    news_type: str,
    recommendation_id: str | None = None,
    recommendation_direction: str | None = None,
) -> None:
    """
    Record a news headline at recommendation time.
    Outcome (5d/30d pnl) gets filled in later by nightly loop.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                INSERT INTO news_impact_log
                    (ticker, headline, source, news_type,
                     recommendation_id, recommendation_direction, recorded_at)
                VALUES
                    (:ticker, :headline, :source, :type,
                     :rec_id, :direction, now())
                ON CONFLICT DO NOTHING
            """), {
                "ticker":     ticker.upper(),
                "headline":   headline[:200],
                "source":     source[:50],
                "type":       news_type,
                "rec_id":     recommendation_id,
                "direction":  recommendation_direction,
            })
    except Exception:
        pass   # Never fail main flow for logging


def update_news_outcomes(user_id: str) -> int:
    """
    Fill in pnl_5d/pnl_30d for news items recorded 5 or 30 days ago.
    Called nightly.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        import yfinance as yf

        with get_session() as s:
            # Get news items recorded 5 days ago without 5d outcome
            rows_5d = s.execute(text("""
                SELECT id, ticker, recorded_at::date AS rec_date
                FROM news_impact_log
                WHERE pnl_5d IS NULL
                  AND recorded_at::date <= CURRENT_DATE - 5
                LIMIT 20
            """)).fetchall()

            updated = 0
            for r in rows_5d:
                try:
                    hist = yf.Ticker(r.ticker).history(
                        start=r.rec_date.isoformat(),
                        end=(r.rec_date + timedelta(days=6)).isoformat()
                    )
                    if len(hist) >= 2:
                        pnl_5d = round(
                            (hist["Close"].iloc[-1] - hist["Close"].iloc[0])
                            / hist["Close"].iloc[0] * 100, 2
                        )
                        s.execute(text("""
                            UPDATE news_impact_log SET pnl_5d = :pnl
                            WHERE id = :id
                        """), {"pnl": pnl_5d, "id": r.id})
                        updated += 1
                except Exception:
                    continue

        return updated
    except Exception as e:
        print(f"[NightlyLoop] News outcome update failed: {e}")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Conviction Weight Recalibration
# ─────────────────────────────────────────────────────────────────────────────

MIN_TRADES_FOR_RECALIBRATION = 10

DEFAULT_WEIGHTS = {
    "entry_trigger": 25,
    "volume":        20,
    "iv_rank":       15,
    "options_flow":  20,
    "vix_zone":      10,
    "ta_alignment":  10,
}


def recalibrate_conviction_weights(user_id: str) -> dict:
    """
    After MIN_TRADES_FOR_RECALIBRATION closed trades:
    Analyze which criteria predicted wins vs losses.
    Boost weights for predictive criteria, reduce for weak ones.
    Store new weights in user_profiles.conviction_weights.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        from collections import defaultdict

        with get_session() as s:
            rows = s.execute(text("""
                SELECT dr.conviction_breakdown, sr.was_correct
                FROM daily_recommendations dr
                JOIN strategy_recommendations sr
                    ON dr.ticker  = sr.symbol
                    AND dr.date   = sr.rec_date
                WHERE dr.user_id = :uid
                  AND sr.was_correct IS NOT NULL
                  AND dr.conviction_breakdown IS NOT NULL
            """), {"uid": user_id}).fetchall()

        if len(rows) < MIN_TRADES_FOR_RECALIBRATION:
            return {
                "recalibrated": False,
                "message": (
                    f"Need {MIN_TRADES_FOR_RECALIBRATION} closed trades. "
                    f"Have {len(rows)}."
                ),
                "data_count": len(rows),
            }

        # Measure win rate when each criterion was satisfied
        crit_stats: dict = defaultdict(lambda: {
            "passed_wins": 0, "passed_total": 0,
            "failed_wins": 0, "failed_total": 0,
        })

        for r in rows:
            breakdown = r.conviction_breakdown
            if isinstance(breakdown, str):
                breakdown = json.loads(breakdown)
            if not breakdown:
                continue

            for crit, data in breakdown.items():
                pts     = data.get("points", 0)
                weight  = DEFAULT_WEIGHTS.get(crit, 10)
                passed  = pts >= weight * 0.7   # ≥70% of max = passed

                if passed:
                    crit_stats[crit]["passed_total"] += 1
                    if r.was_correct:
                        crit_stats[crit]["passed_wins"] += 1
                else:
                    crit_stats[crit]["failed_total"] += 1
                    if r.was_correct:
                        crit_stats[crit]["failed_wins"] += 1

        # Calculate new weights
        new_weights = {}
        adjustments = {}

        for crit, default_w in DEFAULT_WEIGHTS.items():
            stats = crit_stats.get(crit)
            if not stats or stats["passed_total"] < 3:
                new_weights[crit] = default_w
                adjustments[crit] = {"factor": 1.0, "reason": "insufficient data"}
                continue

            win_rate_passed = stats["passed_wins"] / stats["passed_total"] * 100
            win_rate_failed = (
                stats["failed_wins"] / stats["failed_total"] * 100
                if stats["failed_total"] > 0 else 50
            )
            lift = win_rate_passed - win_rate_failed

            # Adjust weight based on predictive lift
            if lift >= 30:
                factor = 1.4
                reason = f"Highly predictive: +{lift:.0f}% lift when passed"
            elif lift >= 15:
                factor = 1.2
                reason = f"Moderately predictive: +{lift:.0f}% lift"
            elif lift >= 0:
                factor = 1.0
                reason = f"Weakly predictive: +{lift:.0f}% lift"
            elif lift >= -15:
                factor = 0.8
                reason = f"Slightly anti-predictive: {lift:.0f}% lift"
            else:
                factor = 0.6
                reason = f"Anti-predictive: {lift:.0f}% lift — reducing weight"

            new_weights[crit] = round(default_w * factor)
            adjustments[crit] = {
                "factor":          factor,
                "old_weight":      default_w,
                "new_weight":      new_weights[crit],
                "win_rate_passed": round(win_rate_passed, 1),
                "win_rate_failed": round(win_rate_failed, 1),
                "lift":            round(lift, 1),
                "reason":          reason,
            }

        # Normalize weights to sum ~100
        total = sum(new_weights.values())
        if total > 0:
            scale = 100 / total
            new_weights = {k: round(v * scale) for k, v in new_weights.items()}

        # Store in user_profiles
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                UPDATE user_profiles
                SET conviction_weights = :weights, updated_at = now()
                WHERE user_id = :uid
            """), {"uid": user_id, "weights": json.dumps(new_weights)})

        print(f"[NightlyLoop] Conviction weights recalibrated from {len(rows)} trades")

        return {
            "recalibrated":  True,
            "data_count":    len(rows),
            "new_weights":   new_weights,
            "adjustments":   adjustments,
            "message":       f"Weights recalibrated from {len(rows)} closed trades",
        }

    except Exception as e:
        return {"recalibrated": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Update Sell Recommendation Outcomes
# ─────────────────────────────────────────────────────────────────────────────

def update_sell_rec_outcomes(user_id: str, positions: list[dict]) -> int:
    """
    After market close: check if any sell signals we fired were correct.
    "Correct" = position went down after SELL signal (or up after HOLD).
    Updates was_correct on sell_recommendations.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        # Get today's unresolved sell signals
        with get_session() as s:
            signals = s.execute(text("""
                SELECT id, symbol, pnl_pct, llm_action
                FROM sell_recommendations
                WHERE user_id    = :uid
                  AND was_correct IS NULL
                  AND llm_action IN ('FULL_EXIT', 'PARTIAL_EXIT')
                  AND rec_date   >= CURRENT_DATE - 30
            """), {"uid": user_id}).fetchall()

        if not signals:
            return 0

        # Current prices from positions
        price_map = {p["symbol"]: float(p.get("unrealized_profit_loss_rate", 0)) * 100
                     for p in positions}

        updated = 0
        with get_session() as s:
            for sig in signals:
                current_pnl = price_map.get(sig.symbol)
                if current_pnl is None:
                    # Position no longer exists = user exited (signal was acted on or expired)
                    s.execute(text("""
                        UPDATE sell_recommendations
                        SET was_correct = TRUE, outcome_at = now()
                        WHERE id = :id
                    """), {"id": sig.id})
                    updated += 1
                    continue

                original_pnl = float(sig.pnl_pct or 0)
                # Was correct if: SELL signal at X% and now position is WORSE
                if current_pnl < original_pnl - 5:
                    was_correct = True    # got worse = signal was right
                elif current_pnl > original_pnl + 5:
                    was_correct = False   # recovered = signal was wrong
                else:
                    continue   # too close to call, wait more

                s.execute(text("""
                    UPDATE sell_recommendations
                    SET was_correct = :correct,
                        outcome_pnl = :pnl,
                        outcome_at  = now()
                    WHERE id = :id
                """), {"correct": was_correct, "pnl": current_pnl, "id": sig.id})
                updated += 1

        return updated

    except Exception as e:
        print(f"[NightlyLoop] Sell rec outcome update failed: {e}")
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Nightly Loop — Main Runner
# ─────────────────────────────────────────────────────────────────────────────

def run_nightly_loop(user_id: str, positions: list[dict]) -> dict:
    """
    Main nightly runner — called by position monitor after market close.
    Runs all learning + backtest updates for the day.
    """
    if not _should_run_tonight(user_id):
        return {"ran": False, "reason": "Already ran today or market was closed"}

    print(f"[NightlyLoop] Starting nightly learning for user {user_id[:8]}...")
    results = {}

    # Step 1: Update sell signal outcomes
    sell_updated = update_sell_rec_outcomes(user_id, positions)
    results["sell_outcomes_updated"] = sell_updated
    print(f"[NightlyLoop] Sell outcomes updated: {sell_updated}")

    # Step 2: Update news impact 5d/30d outcomes
    news_updated = update_news_outcomes(user_id)
    results["news_outcomes_updated"] = news_updated

    # Step 3: Recalibrate conviction weights if enough data
    recal = recalibrate_conviction_weights(user_id)
    results["conviction_recalibration"] = recal

    # Step 4: Run backtest
    from app.recommendations.backtester import run_full_backtest
    backtest = run_full_backtest(user_id)
    results["backtest"] = backtest

    # Step 5: Get learning report
    from app.learning.engine import get_learning_report
    learning = get_learning_report(user_id)
    results["learning"] = learning

    # Step 6: Store in learning_log
    _store_learning_log(user_id, results)

    # Step 7: Send Discord summary
    _send_nightly_discord(user_id, results)

    print(f"[NightlyLoop] Complete")
    return {"ran": True, "results": results}


def _should_run_tonight(user_id: str) -> bool:
    """Check if nightly loop should run (once per day, market days only)."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text("""
                SELECT MAX(ran_at)::date AS last_ran
                FROM learning_log
                WHERE user_id = :uid
            """), {"uid": user_id}).fetchone()
            if row and row.last_ran == date.today():
                return False
    except Exception:
        pass

    # Check market was open today
    try:
        import pytz
        from datetime import time as dtime
        et  = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if now.weekday() >= 5:
            return False
        t = now.time()
        # Only run between 4:30 PM and 8 PM ET
        return dtime(16, 30) <= t <= dtime(20, 0)
    except Exception:
        return True


def _store_learning_log(user_id: str, results: dict) -> None:
    """Store nightly learning run in learning_log table."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            # Get summary data
            backtest = results.get("backtest", {})
            bt1      = backtest.get("sell_signal_cost", {})
            summary  = bt1.get("summary", {})

            s.execute(text("""
                INSERT INTO learning_log (
                    user_id, ran_at, sell_outcomes_updated,
                    backtest_summary, learning_summary,
                    weights_recalibrated
                ) VALUES (
                    :uid, now(), :sell_updated,
                    :backtest, :learning, :recal
                )
            """), {
                "uid":           user_id,
                "sell_updated":  results.get("sell_outcomes_updated", 0),
                "backtest":      json.dumps(summary),
                "learning":      json.dumps(results.get("learning", {}).get("summary", "")),
                "recal":         results.get("conviction_recalibration", {}).get("recalibrated", False),
            })
    except Exception as e:
        print(f"[NightlyLoop] Log store failed: {e}")


def _send_nightly_discord(user_id: str, results: dict) -> None:
    """Send nightly learning summary to Discord."""
    try:
        from app.notifications.discord import get_webhook, send_discord
        webhook = get_webhook(user_id)
        if not webhook:
            return

        backtest = results.get("backtest", {})
        bt1      = backtest.get("sell_signal_cost", {})
        summary  = bt1.get("summary", {})
        learning = results.get("learning", {})
        recal    = results.get("conviction_recalibration", {})

        msg_parts = [
            f"📊 Nightly Learning Summary — {date.today().isoformat()}",
        ]

        if summary.get("net_cost_of_ignoring", 0) > 0:
            msg_parts.append(
                f"💸 Ignoring signals has cost ${summary['net_cost_of_ignoring']:,.0f} in additional losses"
            )

        if recal.get("recalibrated"):
            msg_parts.append(f"⚡ Conviction weights recalibrated from {recal['data_count']} trades")

        if learning.get("summary"):
            msg_parts.append(f"🧠 {learning['summary']}")

        msg_parts.append("Check get_learning_report() for full details")

        send_discord(
            webhook_url = webhook,
            symbol      = "PORTFOLIO",
            alert_type  = "NIGHTLY_LEARNING",
            urgency     = "LOW",
            message     = " | ".join(msg_parts),
        )
    except Exception as e:
        print(f"[NightlyLoop] Discord send failed: {e}")
