"""
C10 Prediction Tracker (W15).

Tracks every recommendation → execution → outcome to calculate win rate
and improve future recommendations.

Uses EXISTING tables — no new schema needed:
    tracked_positions    → execution log (entry_price, qty, target, stop)
    sell_recommendations → sell signal outcomes (user_acted, outcome_pnl, was_correct)
    user_profiles        → learned preferences (best_strategy, risk_tolerance)

Flow:
    1. Claude recommends trade (strategy engine)
    2. User says "yes I executed" → confirm_execution() → tracked_positions
    3. Position closes → log_exit() → tracked_positions + sell_recommendations
    4. get_learning_report() → reads both tables → win rate + learnings
    5. update_user_profile() → writes learnings to user_profiles
"""
from datetime import datetime, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# Confirm Execution (user bought the recommended trade)
# ─────────────────────────────────────────────────────────────────────────────

def confirm_execution(
    user_id: str,
    symbol: str,
    entry_price: float,
    qty: int,
    recommendation_id: str | None = None,
    source: str = "recommendation",
) -> dict:
    """
    User confirms they executed a recommended trade.
    Fully idempotent — same symbol + price + date = always 1 row.
    Tomorrow same price = new row. Different price today = new row.

    source: 'recommendation' (default, real manual fills) or
    'auto_paper' (the automated paper-trade-open job, which
    deliberately wants MULTIPLE simultaneous positions on the same
    ticker/day — one per window/budget combo in its grid, not one per
    day). The dedup guard below is scoped to (symbol, day, source, qty)
    rather than just (symbol, day) so real fills and paper-trade combos
    never collide with each other.

    For source='auto_paper' specifically, this guard is skipped
    entirely — always insert a new row, never dedupe/reuse. The
    paper-trade-open job deliberately wants MULTIPLE simultaneous
    positions on the same ticker/day (one per window/budget combo in
    its grid), and a (symbol, day, qty) key still isn't granular enough
    to tell them apart: multiple windows can independently pick the
    identical ticker/strategy (confirmed live — every window converged
    on the same SPY IRON_CONDOR during a quiet-market test run), and
    margin-based strategies like iron condors often size to the same
    contract count across several different budgets, so a qty-based
    key silently collapsed 20 distinct combos down to 4 real tracked
    positions the first time this ran. The job's own loop is the sole
    caller for this source and controls not calling it twice for the
    same combo within one run — an actual double-run (e.g. a mid-run
    restart) is the same in-process-scheduler reliability gap already
    tracked elsewhere, not something this guard can solve anyway.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        with get_session() as s:

            # Guard: one tracked position per (symbol, day, source, qty) —
            # 'recommendation' (real fills) only. See docstring above for
            # why auto_paper skips this entirely.
            if source == "recommendation":
                existing = s.execute(text("""
                    SELECT id FROM tracked_positions
                    WHERE user_id=:uid AND symbol=:sym AND source=:source AND qty=:qty
                    AND entry_date=CURRENT_DATE AND is_active=TRUE
                """), {"uid": user_id, "sym": symbol, "source": source, "qty": qty}).fetchone()
                if existing:
                    return {"status": "already_tracked", "id": str(existing.id)}

            # 1. Find today's daily_recommendations row for this ticker —
            #    the table the CURRENT engine actually writes to. Prefer an
            #    exact recommendation_id if the caller passed one; otherwise
            #    match on ticker + today's date (most recent if several).
            if not recommendation_id:
                # Options commonly have MULTIPLE recs for the same ticker on
                # the same day (e.g. SPY as both IRON_CONDOR and
                # DEBIT_CALL_SPREAD) — "most recently created" isn't a
                # reliable way to pick the right one. Disambiguate using the
                # price the user actually reports filling at: it should be
                # close to ONE specific rec's entry_debit (options) or
                # entry_zone_low (stock), and meaningfully off from the
                # others. Falls back naturally to "closest anyway" if there's
                # only one candidate, or none are close (COALESCE→0 pushes
                # unpriced/legacy rows to the bottom, doesn't crash on them).
                row = s.execute(text("""
                    SELECT id, ABS(COALESCE(entry_debit, entry_zone_low, 0) - :entry) AS diff
                    FROM daily_recommendations
                    WHERE user_id = :uid AND ticker = :sym AND date = CURRENT_DATE
                    ORDER BY diff ASC, created_at DESC
                    LIMIT 1
                """), {"uid": user_id, "sym": symbol.upper(), "entry": entry_price}).fetchone()
                if row:
                    recommendation_id = str(row.id)
                    print(f"[Tracker] Matched daily_recommendations for {symbol} "
                          f"(price diff: {row.diff})")
                else:
                    print(f"[Tracker] No daily_recommendations match for {symbol} today "
                          f"— fill will still be tracked, just unlinked to a specific rec")

            # 2. Mark that recommendation as executed with the REAL fill
            #    details — separate from the recommended entry/target/stop,
            #    which stay untouched as the original thesis record.
            if recommendation_id:
                s.execute(text("""
                    UPDATE daily_recommendations
                    SET user_executed      = TRUE,
                        actual_entry_price = :entry,
                        actual_qty         = :qty,
                        executed_at        = now()
                    WHERE id = :rid AND user_id = :uid
                """), {
                    "rid": recommendation_id, "uid": user_id,
                    "entry": entry_price, "qty": qty,
                })
                print(f"[Tracker] Marked daily_recommendations {recommendation_id[:8]} as executed")

            # Pull REAL target_pct/stop_pct from the actual daily_recommendations
            # row for this ticker today — the table the current engine
            # (smart_engine/rescan_engine/daily_engine) actually writes to —
            # instead of hardcoding a generic +20%/-40%. Falls back to those
            # defaults only if no matching recommendation exists at all
            # (a fully manual, off-system trade with nothing to link to).
            real_target_pct, real_stop_pct = 20.0, -40.0
            try:
                rec_row = s.execute(text("""
                    SELECT target_pct, stop_pct
                    FROM daily_recommendations
                    WHERE user_id = :uid AND ticker = :sym AND date = CURRENT_DATE
                    ORDER BY created_at DESC
                    LIMIT 1
                """), {"uid": user_id, "sym": symbol.upper()}).fetchone()
                if rec_row and rec_row.target_pct is not None and rec_row.stop_pct is not None:
                    real_target_pct = float(rec_row.target_pct)
                    real_stop_pct   = float(rec_row.stop_pct)
                    print(f"[Tracker] Real target/stop for {symbol}: "
                          f"+{real_target_pct}% / {real_stop_pct}%")
                else:
                    print(f"[Tracker] No daily_recommendations match for {symbol} today "
                          f"— using generic +20%/-40% defaults")
            except Exception as e:
                print(f"[Tracker] target/stop lookup failed, using defaults: {e}")

            # 3. tracked_positions — update if exists today at this price
            #    (+ source, so an auto_paper combo never overwrites a real
            #    fill or a different combo just because the price matches),
            #    else insert. auto_paper skips this lookup entirely and
            #    always inserts fresh — see function docstring.
            existing = None
            if source == "recommendation":
                existing = s.execute(text("""
                    SELECT id FROM tracked_positions
                    WHERE user_id      = :uid
                      AND symbol       = :sym
                      AND entry_date   = CURRENT_DATE
                      AND entry_price  = :entry
                      AND source       = :source
                      AND is_active    = TRUE
                """), {"uid": user_id, "sym": symbol,
                       "entry": entry_price, "source": source}).fetchone()

            if existing:
                s.execute(text("""
                    UPDATE tracked_positions
                    SET qty                = :qty,
                        source             = :source,
                        check_interval_min = 15,
                        is_active          = TRUE
                    WHERE id = :id
                """), {"id": existing.id, "qty": qty, "source": source})
                tracked_position_id = str(existing.id)
                print(f"[Tracker] Updated tracked_position for {symbol}")
            else:
                new_row = s.execute(text("""
                    INSERT INTO tracked_positions (
                        user_id, symbol, source, entry_date,
                        entry_price, qty, target_pct, stop_pct,
                        check_interval_min
                    ) VALUES (
                        :uid, :sym, :source, CURRENT_DATE,
                        :entry, :qty, :tgt, :stp, 15
                    )
                    RETURNING id
                """), {
                    "uid": user_id, "sym": symbol, "source": source,
                    "entry": entry_price, "qty": qty,
                    "tgt": real_target_pct, "stp": real_stop_pct,
                }).fetchone()
                tracked_position_id = str(new_row.id) if new_row else None
                print(f"[Tracker] Created tracked_position for {symbol}")

        total_cost = round(entry_price * qty * 100, 2)
        return {
            "confirmed":           True,
            "symbol":              symbol,
            "entry_price":         entry_price,
            "qty":                 qty,
            "total_cost":          total_cost,
            "recommendation_id":   recommendation_id,
            "tracked_position_id": tracked_position_id,
            "target_pct":          real_target_pct,
            "stop_pct":            real_stop_pct,
            "monitoring":        "every 15 min during market hours",
            "message": (
                f"✅ Logged: {qty} {symbol} contracts at ${entry_price} "
                f"(total ${total_cost:,.0f}). Monitoring every 15 min. "
                f"Discord alert fires at +{real_target_pct}% target "
                f"or {real_stop_pct}% stop."
            ),
        }

    except Exception as e:
        return {"confirmed": False, "error": str(e)}


def log_exit(
    user_id: str,
    symbol: str,
    exit_price: float,
    exit_reason: str = "MANUAL",
) -> dict:
    """
    Log when user exits a position.
    Updates tracked_positions + links to sell_recommendations outcome.

    exit_reason: STOP_LOSS / TAKE_PROFIT / MANUAL / EXPIRED / ROLLED
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            # Get entry details
            pos = s.execute(text("""
                SELECT id, entry_price, qty, target_pct, stop_pct
                FROM tracked_positions
                WHERE user_id = :uid AND symbol = :sym AND is_active = TRUE
                ORDER BY entry_date DESC LIMIT 1
            """), {"uid": user_id, "sym": symbol.upper()}).fetchone()

            if not pos:
                return {"logged": False, "error": f"No active position found for {symbol}"}

            # Calculate P&L
            entry_price = float(pos.entry_price)
            qty         = float(pos.qty)
            pnl_pct     = (exit_price - entry_price) / entry_price * 100
            pnl_abs     = (exit_price - entry_price) * qty
            won         = pnl_pct > 0

            # Close tracked_position
            s.execute(text("""
                UPDATE tracked_positions
                SET is_active  = FALSE,
                    exit_date  = CURRENT_DATE,
                    exit_price = :ep,
                    exit_reason = :reason
                WHERE id = :id
            """), {"ep": exit_price, "reason": exit_reason, "id": pos.id})

            # Update most recent sell_recommendation for this symbol
            s.execute(text("""
                UPDATE sell_recommendations
                SET user_acted     = TRUE,
                    exit_price     = :ep,
                    outcome_pnl    = :pnl,
                    outcome_at     = now(),
                    was_correct    = :won
                WHERE user_id = :uid AND symbol = :sym
                  AND recommended_at = (
                      SELECT MAX(recommended_at) FROM sell_recommendations
                      WHERE user_id = :uid AND symbol = :sym
                  )
            """), {
                "ep": exit_price, "pnl": round(pnl_abs, 2),
                "won": won, "uid": user_id, "sym": symbol.upper(),
            })

            # Ground-truth close on the matching daily_recommendations row —
            # this is the real, actually-realized outcome, distinct from
            # mark_to_market's paper current_pnl_pct (computed for every
            # rec whether filled or not).
            s.execute(text("""
                UPDATE daily_recommendations
                SET exit_price     = :ep,
                    exit_reason    = :reason,
                    closed_at      = now(),
                    actual_pnl     = :pnl_abs,
                    actual_pnl_pct = :pnl_pct,
                    was_correct    = :won
                WHERE user_id = :uid AND ticker = :sym
                  AND user_executed = TRUE AND closed_at IS NULL
                ORDER BY executed_at DESC
                LIMIT 1
            """), {
                "uid": user_id, "sym": symbol.upper(),
                "ep": exit_price, "reason": exit_reason,
                "pnl_abs": round(pnl_abs, 2), "pnl_pct": round(pnl_pct, 1),
                "won": won,
            })

        result = {
            "logged":      True,
            "symbol":      symbol.upper(),
            "exit_price":  exit_price,
            "entry_price": entry_price,
            "pnl_pct":     round(pnl_pct, 1),
            "pnl_abs":     round(pnl_abs, 2),
            "outcome":     "WIN ✅" if won else "LOSS ❌",
            "exit_reason": exit_reason,
        }

        print(f"[Tracker] Exit logged: {symbol} {pnl_pct:+.1f}% (${pnl_abs:+,.0f}) — {exit_reason}")
        return result

    except Exception as e:
        print(f"[Tracker] log_exit failed: {e}")
        return {"logged": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Accuracy Report
# ─────────────────────────────────────────────────────────────────────────────

def get_accuracy_report(user_id: str, days_back: int = 90) -> dict:
    """
    Deprecated — use get_learning_report() from app.learning.engine instead.
    This is kept as a thin alias for backwards compatibility.
    """
    try:
        from app.learning.engine import get_learning_report
        return get_learning_report(user_id)
    except Exception as e:
        return {"error": str(e), "message": "Use get_learning_report() directly"}


def format_accuracy_report(report: dict) -> str:
    """Format accuracy report for Claude Desktop display."""
    if report.get("error"):
        return f"❌ Error: {report['error']}"

    if report["total_trades"] == 0:
        return (
            "## Prediction Tracker\n\n"
            "No closed trades recorded yet.\n\n"
            "After executing a recommendation, say:\n"
            "  *'I bought NVDA at $210, 6 contracts'*\n\n"
            "After closing, say:\n"
            "  *'I sold NVDA at $195'*"
        )

    lines = ["## Prediction Tracker — Accuracy Report"]
    lines.append(f"**Period:** Last {report['period_days']} days\n")

    lines.append("### Trade Performance")
    lines.append(f"**Win Rate:** {report['win_rate']}% ({report['wins']}W / {report['losses']}L)")
    lines.append(f"**Total P&L:** ${report['total_pnl']:+,.2f}")
    lines.append(f"**Avg Win:** {report['avg_win_pct']:+.1f}% | **Avg Loss:** {report['avg_loss_pct']:+.1f}%")
    if report.get("avg_hold_days"):
        lines.append(f"**Avg Hold:** {report['avg_hold_days']} days")

    if report.get("sell_signals_tracked", 0) > 0:
        lines.append(f"\n**Sell Signal Accuracy:** {report['sell_signal_accuracy']}% ({report['sell_signals_tracked']} signals tracked)")

    if report.get("strategy_performance"):
        lines.append("\n### By Strategy")
        for name, stats in sorted(
            report["strategy_performance"].items(),
            key=lambda x: x[1]["win_rate"], reverse=True
        ):
            badge = "🥇" if name == report.get("best_strategy") else \
                    "⚠️" if name == report.get("worst_strategy") else "  "
            lines.append(f"{badge} **{name}**: {stats['win_rate']}% win rate | {stats['trades']} trades | avg {stats['avg_pnl']:+.1f}%")

    if report.get("ignored_signals"):
        lines.append("\n### Ignored Signals — Cost of Not Acting")
        for ig in report["ignored_signals"]:
            lines.append(f"  ⚠️ **{ig['symbol']}**: ignored {ig['times_ignored']}x | current P&L: {ig['last_pnl']:+.1f}%")

    if report.get("recent_trades"):
        lines.append("\n### Recent Trades")
        for t in report["recent_trades"][:5]:
            icon = "✅" if t["won"] else "❌"
            lines.append(f"  {icon} {t['symbol']} {t['pnl_pct']:+.1f}% (${t['pnl_abs']:+,.0f}) — {t['exit_reason']}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Update User Profile from Learnings
# ─────────────────────────────────────────────────────────────────────────────

def update_user_profile_from_outcomes(user_id: str) -> bool:
    """
    Update user_profiles table with learnings from trade outcomes.
    Called after enough trades accumulate (5+).
    """
    try:
        from app.learning.engine import get_learning_report as _lr; report = _lr(user_id)
        if report.get("total_trades", 0) < 3:
            return False

        from sqlalchemy import text
        from app.db.session import get_session
        import json

        with get_session() as s:
            s.execute(text("""
                INSERT INTO user_profiles (user_id, best_performing_strategy,
                    worst_performing_strategy, avg_hold_days, updated_at)
                VALUES (:uid, :best, :worst, :hold, now())
                ON CONFLICT (user_id)
                DO UPDATE SET
                    best_performing_strategy  = EXCLUDED.best_performing_strategy,
                    worst_performing_strategy = EXCLUDED.worst_performing_strategy,
                    avg_hold_days             = EXCLUDED.avg_hold_days,
                    updated_at                = now()
            """), {
                "uid":   user_id,
                "best":  report.get("best_strategy"),
                "worst": report.get("worst_strategy"),
                "hold":  report.get("avg_hold_days"),
            })

        print(f"[Tracker] User profile updated from {report['total_trades']} trades")
        return True
    except Exception as e:
        print(f"[Tracker] Profile update failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Trade History
# ─────────────────────────────────────────────────────────────────────────────

def get_trade_history(user_id: str, days_back: int = 90) -> list[dict]:
    """Full trade history — open and closed positions."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT symbol, source, entry_date, entry_price, qty,
                       target_pct, stop_pct, exit_date, exit_price,
                       exit_reason, is_active, llm_entry_note
                FROM tracked_positions
                WHERE user_id = :uid
                  AND entry_date >= CURRENT_DATE - :days
                ORDER BY entry_date DESC
            """), {"uid": user_id, "days": days_back}).fetchall()

            history = []
            for r in rows:
                pnl_pct = None
                if r.entry_price and r.exit_price:
                    pnl_pct = round(
                        (float(r.exit_price) - float(r.entry_price)) / float(r.entry_price) * 100, 1
                    )
                history.append({
                    "symbol":      r.symbol,
                    "source":      r.source,
                    "status":      "OPEN" if r.is_active else "CLOSED",
                    "entry_date":  str(r.entry_date),
                    "entry_price": float(r.entry_price) if r.entry_price else None,
                    "qty":         r.qty,
                    "target_pct":  float(r.target_pct) if r.target_pct else None,
                    "stop_pct":    float(r.stop_pct) if r.stop_pct else None,
                    "exit_date":   str(r.exit_date) if r.exit_date else None,
                    "exit_price":  float(r.exit_price) if r.exit_price else None,
                    "pnl_pct":     pnl_pct,
                    "exit_reason": r.exit_reason,
                    "note":        r.llm_entry_note,
                })
            return history
    except Exception as e:
        return [{"error": str(e)}]