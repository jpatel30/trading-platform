"""
Weekly strategy review (Phase 6).

Turns a week of accumulated paper-trade outcomes (Phase 4's opens,
Phase 5's closes) into specific, falsifiable statistics about which
signals actually predicted wins — split out from which didn't.

CRITICAL CONSTRAINT: every statistic here is a plain Python aggregation
over already-closed daily_recommendations rows (counts, means, win
rates). No LLM ever estimates or computes a number. The one optional LLM
call at the end is handed ONLY the already-computed bucket numbers and
asked to phrase them as a sentence — never given raw trade rows, never
asked to produce a number itself. Its output (llm_summary) is for human
reading only right now — nothing downstream reads it back into the
recommendation pipeline yet.
"""
import json
from datetime import date, datetime, timedelta, timezone

MIN_SAMPLE_SIZE = 5

# Provisional — no real iv_5day_trend data exists anywhere in the system
# yet to calibrate against (every paper_trade_context row so far has it
# NULL; SPY/QQQ haven't accumulated 5 days of iv_history yet). +5%
# relative rate-of-change is a common practitioner cutoff for "IV is
# actually expanding" vs day-to-day noise. Revisit once a few weeks of
# real iv_5day_trend values exist to check this empirically.
IV_EXPANDING_THRESHOLD_PCT = 5.0


# ─────────────────────────────────────────────────────────────────────────────
# Bucket definitions — each maps a joined row to a label (or None to
# exclude the row from that particular split, e.g. missing data). Adding
# a new bucket is one more entry in BUCKET_GROUPS, not a rewrite. This
# generalizes a plain boolean predicate (label is just "True"/"False")
# to also cover the multi-way splits this review needs (window_length,
# which_strategy_rule_fired, the conviction x intraday cross-tab).
# ─────────────────────────────────────────────────────────────────────────────

def _bucket_strategy_rule(r):
    return r.which_strategy_rule_fired or None


def _bucket_oi_persistence(r):
    if r.oi_max_days is None:
        return None
    return "10plus_days" if r.oi_max_days >= 10 else "under_10_days"


def _bucket_iv_trend(r):
    if r.iv_5day_trend is None:
        return None
    return "expanding" if float(r.iv_5day_trend) > IV_EXPANDING_THRESHOLD_PCT else "flat_or_contracting"


def _any_rule_fired(signal) -> bool | None:
    if not signal:
        return None
    return bool(signal.get("any_rule_fired"))


def _bucket_intraday_5min(r):
    fired = _any_rule_fired(r.intraday_5min_signal)
    return None if fired is None else ("confirmed" if fired else "not_confirmed")


def _bucket_intraday_15min(r):
    fired = _any_rule_fired(r.intraday_15min_signal)
    return None if fired is None else ("confirmed" if fired else "not_confirmed")


def _bucket_window_length(r):
    d = r.trading_window_days
    if d is None:
        return None
    if d <= 7:
        return "short_leq7"
    if d <= 30:
        return "mid_8_30"
    return "long_gt30"


def _bucket_conviction_x_5min(r):
    tier = r.conviction_tier
    fired = _any_rule_fired(r.intraday_5min_signal)
    if not tier or fired is None:
        return None
    return f"{tier}_{'confirmed' if fired else 'not_confirmed'}"


BUCKET_GROUPS = [
    ("strategy_rule",              _bucket_strategy_rule),
    ("oi_persistence",             _bucket_oi_persistence),
    ("iv_trend",                   _bucket_iv_trend),
    ("intraday_5min_confirmed",    _bucket_intraday_5min),
    ("intraday_15min_confirmed",   _bucket_intraday_15min),
    ("window_length",              _bucket_window_length),
    ("conviction_x_5min_confirmed", _bucket_conviction_x_5min),
]


def _bucket_stats(rows: list) -> dict:
    n     = len(rows)
    wins  = sum(1 for r in rows if r.was_correct)
    losses = n - wins
    pnls  = [float(r.actual_pnl_pct) for r in rows if r.actual_pnl_pct is not None]
    return {
        "wins": wins, "losses": losses,
        "win_rate":    round(wins / n * 100, 1) if n else None,
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 1) if pnls else None,
        "sample_size": n,
        "sufficient_sample": n >= MIN_SAMPLE_SIZE,
    }


def _relevant_entry_confirmed(r) -> bool | None:
    """
    Which timeframe's entry-confirmation signal is "the" one that matters
    for a given losing trade. Judgment call: if BOTH 5-min and 15-min
    signals were captured, use 15-min — it's the longer lookback, so a
    real confirmation there is a stronger claim than a 5-min blip. Falls
    back to whichever timeframe was actually captured if only one was.
    Returns None (uncategorizable) if neither was captured at all.
    """
    fired_15 = _any_rule_fired(r.intraday_15min_signal)
    if fired_15 is not None:
        return fired_15
    return _any_rule_fired(r.intraday_5min_signal)


# ─────────────────────────────────────────────────────────────────────────────
# Week range default — the just-completed Mon-Fri.
# ─────────────────────────────────────────────────────────────────────────────

def _default_week_range() -> tuple:
    today = date.today()
    days_since_friday = (today.weekday() - 4) % 7   # Mon=0 ... Sun=6, Fri=4
    week_end   = today - timedelta(days=days_since_friday)
    week_start = week_end - timedelta(days=4)
    return week_start, week_end


def _fetch_joined_rows(user_id: str, week_start: date, week_end: date) -> list:
    from sqlalchemy import text
    from app.db.session import get_session

    with get_session() as s:
        return s.execute(text("""
            SELECT dr.id AS recommendation_id, dr.ticker, dr.date, dr.was_correct,
                   dr.actual_pnl_pct, dr.conviction_tier,
                   ptc.which_strategy_rule_fired, ptc.oi_max_days, ptc.iv_5day_trend,
                   ptc.trading_window_days, ptc.intraday_5min_signal, ptc.intraday_15min_signal
            FROM daily_recommendations dr
            JOIN paper_trade_context ptc ON ptc.recommendation_id = dr.id
            WHERE dr.user_id = :uid
              AND dr.date BETWEEN :ws AND :we
              AND dr.was_correct IS NOT NULL
              AND (dr.excluded_from_stats IS NULL OR dr.excluded_from_stats = FALSE)
        """), {"uid": user_id, "ws": week_start, "we": week_end}).fetchall()


def _upsert_bucket_row(user_id: str, bucket_name: str, week_start: date, week_end: date, stats: dict) -> None:
    from sqlalchemy import text
    from app.db.session import get_session

    with get_session() as s:
        s.execute(text("""
            INSERT INTO strategy_rule_performance (
                user_id, bucket_name, week_start, week_end,
                wins, losses, win_rate, avg_pnl_pct, sample_size, sufficient_sample
            ) VALUES (
                :uid, :bucket, :ws, :we,
                :wins, :losses, :win_rate, :avg_pnl_pct, :n, :sufficient
            )
            ON CONFLICT (user_id, bucket_name, week_start) DO UPDATE SET
                week_end          = EXCLUDED.week_end,
                wins              = EXCLUDED.wins,
                losses            = EXCLUDED.losses,
                win_rate          = EXCLUDED.win_rate,
                avg_pnl_pct       = EXCLUDED.avg_pnl_pct,
                sample_size       = EXCLUDED.sample_size,
                sufficient_sample = EXCLUDED.sufficient_sample,
                computed_at       = now()
        """), {
            "uid": user_id, "bucket": bucket_name, "ws": week_start, "we": week_end,
            "wins": stats["wins"], "losses": stats["losses"],
            "win_rate": stats["win_rate"], "avg_pnl_pct": stats["avg_pnl_pct"],
            "n": stats["sample_size"], "sufficient": stats["sufficient_sample"],
        })


def _upsert_weekly_review_log(
    user_id: str, week_start: date, week_end: date, overall: dict,
    wrong_trade_count: int, wrong_entry_count: int, llm_summary: str | None,
) -> None:
    from sqlalchemy import text
    from app.db.session import get_session

    with get_session() as s:
        s.execute(text("""
            INSERT INTO weekly_review_log (
                user_id, week_start, week_end, total_paper_trades,
                overall_win_rate, overall_avg_pnl_pct,
                wrong_trade_count, wrong_entry_count, llm_summary
            ) VALUES (
                :uid, :ws, :we, :total,
                :win_rate, :avg_pnl_pct,
                :wrong_trade, :wrong_entry, :summary
            )
            ON CONFLICT (user_id, week_start) DO UPDATE SET
                week_end             = EXCLUDED.week_end,
                total_paper_trades   = EXCLUDED.total_paper_trades,
                overall_win_rate     = EXCLUDED.overall_win_rate,
                overall_avg_pnl_pct  = EXCLUDED.overall_avg_pnl_pct,
                wrong_trade_count    = EXCLUDED.wrong_trade_count,
                wrong_entry_count    = EXCLUDED.wrong_entry_count,
                llm_summary          = EXCLUDED.llm_summary,
                created_at           = now()
        """), {
            "uid": user_id, "ws": week_start, "we": week_end,
            "total": overall["sample_size"], "win_rate": overall["win_rate"],
            "avg_pnl_pct": overall["avg_pnl_pct"],
            "wrong_trade": wrong_trade_count, "wrong_entry": wrong_entry_count,
            "summary": llm_summary,
        })


def _log_job_run(job_name: str, started_at, status: str, processed: int, failed: int, details: dict) -> None:
    from sqlalchemy import text
    from app.db.session import get_session

    try:
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
                "details": json.dumps(details, default=str),
            })
    except Exception as e:
        print(f"[WeeklyReview] job_run_log write failed: {e}")


def _generate_llm_summary(overall: dict, bucket_results: dict, timeframe_comparison: dict,
                           wrong_trade_count: int, wrong_entry_count: int) -> str | None:
    """
    ONE LLM call, given ONLY already-computed numbers — never raw trade
    rows. Purely for phrasing; every number in the prompt was already
    computed by _bucket_stats()/plain aggregation above. Human-reading
    only for now — nothing downstream consumes llm_summary yet.
    """
    try:
        from app.utils.config import settings
        import requests as req

        lines = []
        for group_name, labels in bucket_results.items():
            for label, stats in labels.items():
                if stats["sample_size"] == 0:
                    continue
                tag = "insufficient data" if not stats["sufficient_sample"] else f"{stats['win_rate']}%"
                lines.append(
                    f"{group_name}={label}: {stats['wins']}W/{stats['losses']}L, {tag}, "
                    f"n={stats['sample_size']}, avg_pnl_pct={stats['avg_pnl_pct']}"
                )
        bucket_str = "\n".join(lines) if lines else "(no buckets had any data this week)"

        tf5  = timeframe_comparison.get("5min", {})
        tf15 = timeframe_comparison.get("15min", {})

        prompt = f"""Here are ALREADY-COMPUTED real statistics from one week of paper-trade
outcomes ({overall['sample_size']} total closed trades, overall win rate
{overall['win_rate']}%, overall avg pnl {overall['avg_pnl_pct']}%).

BUCKET BREAKDOWN:
{bucket_str}

5-MIN vs 15-MIN INTRADAY ENTRY-TIMING COMPARISON:
  5-min confirmed:  {tf5}
  15-min confirmed: {tf15}

WRONG-TRADE vs WRONG-ENTRY (among losing trades only):
  wrong_trade (no entry-timing confirmation at all): {wrong_trade_count}
  wrong_entry (entry-timing confirmed, still lost):  {wrong_entry_count}

Write a short (4-6 sentence) natural-language summary of what these
numbers say. Rules:
- Use ONLY the numbers given above. Do not invent, estimate, or
  recompute any number.
- Any bucket marked "insufficient data" MUST be described as
  insufficient data (n=X) — never state a percentage for it, never
  editorialize about whether it looks good or bad.
- Explicitly call out the 5-min vs 15-min comparison if both have
  enough data to compare.
- Plain prose, no markdown, no bullet points."""

        payload = {
            "model": settings.ollama_model, "prompt": prompt, "stream": False,
            "options": {"num_predict": 400, "temperature": 0.1},
        }
        r = req.post(f"{settings.ollama_host}/api/generate", json=payload, timeout=60)
        summary = r.json().get("response", "").strip()
        return summary or None
    except Exception as e:
        print(f"[WeeklyReview] LLM summary failed (non-fatal): {e}")
        return None


def run_weekly_strategy_review(user_id: str, week_start: date | None = None, week_end: date | None = None) -> dict:
    started_at = datetime.now(timezone.utc)

    if week_start is None or week_end is None:
        week_start, week_end = _default_week_range()

    rows = _fetch_joined_rows(user_id, week_start, week_end)

    overall = _bucket_stats(rows)

    bucket_results: dict = {}
    for group_name, bucketer in BUCKET_GROUPS:
        by_label: dict = {}
        for r in rows:
            label = bucketer(r)
            if label is None:
                continue
            by_label.setdefault(label, []).append(r)
        group_stats = {label: _bucket_stats(label_rows) for label, label_rows in by_label.items()}
        bucket_results[group_name] = group_stats
        for label, stats in group_stats.items():
            _upsert_bucket_row(user_id, f"{group_name}:{label}", week_start, week_end, stats)

    # Explicit, dedicated 5-min vs 15-min comparison — the empirical
    # answer to "which timeframe is more accurate," surfaced directly
    # rather than left for someone to dig out of the generic bucket dump.
    tf5_confirmed  = bucket_results.get("intraday_5min_confirmed", {}).get("confirmed", _bucket_stats([]))
    tf15_confirmed = bucket_results.get("intraday_15min_confirmed", {}).get("confirmed", _bucket_stats([]))
    if tf5_confirmed["sufficient_sample"] and tf15_confirmed["sufficient_sample"]:
        if tf5_confirmed["win_rate"] > tf15_confirmed["win_rate"]:
            verdict = "5-min confirmation currently correlates with a higher win rate than 15-min."
        elif tf15_confirmed["win_rate"] > tf5_confirmed["win_rate"]:
            verdict = "15-min confirmation currently correlates with a higher win rate than 5-min."
        else:
            verdict = "5-min and 15-min confirmation currently show the same win rate."
    else:
        verdict = "insufficient data on at least one timeframe to compare yet"
    timeframe_comparison = {"5min": tf5_confirmed, "15min": tf15_confirmed, "verdict": verdict}

    # Wrong-trade vs wrong-entry, losses only.
    losing_rows = [r for r in rows if r.was_correct is False]
    wrong_trade_count = 0
    wrong_entry_count = 0
    for r in losing_rows:
        confirmed = _relevant_entry_confirmed(r)
        if confirmed is None:
            continue
        if confirmed:
            wrong_entry_count += 1
        else:
            wrong_trade_count += 1

    llm_summary = _generate_llm_summary(
        overall, bucket_results, timeframe_comparison, wrong_trade_count, wrong_entry_count,
    )

    _upsert_weekly_review_log(user_id, week_start, week_end, overall, wrong_trade_count, wrong_entry_count, llm_summary)

    status = "success"
    _log_job_run(
        "weekly_strategy_review", started_at, status, overall["sample_size"], 0,
        {
            "week_start": str(week_start), "week_end": str(week_end),
            "overall": overall, "bucket_results": bucket_results,
            "timeframe_comparison": timeframe_comparison,
            "wrong_trade_count": wrong_trade_count, "wrong_entry_count": wrong_entry_count,
            "llm_summary": llm_summary, "error_summary": None,
        },
    )

    return {
        "job": "weekly_strategy_review",
        "week_start": week_start, "week_end": week_end,
        "overall": overall,
        "bucket_results": bucket_results,
        "timeframe_comparison": timeframe_comparison,
        "wrong_trade_count": wrong_trade_count, "wrong_entry_count": wrong_entry_count,
        "total_losses": len(losing_rows),
        "llm_summary": llm_summary,
        "status": status,
    }
