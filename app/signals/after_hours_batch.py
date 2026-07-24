"""
After-hours batch job — real day-over-day history for TA, fundamentals,
and insider activity across the whole watchlist.

Same "predictive needs trend, not a snapshot" principle that already
makes OI buildup (signals/oi_flow.py) and velocity (signals/
velocity_tracker.py) valuable, applied to three signal categories that
currently have no storage at all — they're computed fresh on every scan
and thrown away.

iv_history already exists and works correctly for IV specifically (see
rag/context_builder.py's _get_real_iv_rank / _record_iv_history) — do
not touch its schema or its reader. Its actual gap was narrower:
_record_iv_history() only ever ran incidentally, for whichever tickers
happened to get enriched during a manual scan, not the whole watchlist
every day. This job closes that gap by calling _build_iv_context()
(which calls _record_iv_history() internally) for every watchlist
ticker, not just enriched ones — same call as any other consumer, no
special-casing.

Field note: get_technical_profile() as it exists today computes ema20
(single EMA) and rsi_14 with a numeric macd_signal (the MACD signal
line value) — not the ema9/ema21/text-macd_signal fields once assumed
here. Storing the real fields it actually computes, per "reuse it
exactly as-is", rather than inventing indicators that don't exist.
"""
from datetime import datetime, timedelta, timezone

JOB_NAME = "after_hours_batch"


def run_after_hours_batch(user_id: str) -> dict:
    """
    For every ticker in this user's scan universe (get_scan_universe,
    watchlist_mode=default_plus_mine — same broker-independent universe
    source as everything else): record today's TA/fundamentals/insider
    (new — ticker_daily_snapshot) and IV (existing — iv_history).

    Resilient per ticker — one ticker's failure (bad data, API error)
    never aborts the rest, same try/except-per-ticker pattern quick_scan.py
    and smart_stock_scan.py already use. Always writes a job_run_log row,
    even on a total crash, so "did this run today" is never silently
    unanswerable.
    """
    from app.scanner.universe import get_scan_universe

    started_at = datetime.now(timezone.utc)
    tickers: list[str] = []
    processed = 0
    failed = 0
    failures: list[str] = []

    try:
        tickers = get_scan_universe(user_id, watchlist_mode="default_plus_mine")

        # VIX once for the whole run (IV rank interpretation needs it) —
        # not worth a call per ticker, same shared-fetch-once approach
        # rescan_engine.py already uses for its own per-scan VIX context.
        vix = 17.0
        try:
            from app.rag.context_builder import _build_vix_context
            vix = _build_vix_context().get("current") or 17.0
        except Exception:
            pass

        from_date = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
        to_date   = datetime.now().strftime("%Y-%m-%d")

        for ticker in tickers:
            try:
                _snapshot_one_ticker(ticker, from_date, to_date, vix)
                processed += 1
            except Exception as e:
                failed += 1
                failures.append(f"{ticker}: {e}")
                print(f"[AfterHoursBatch] {ticker} failed: {e}")
                continue

        status = "success" if failed == 0 else ("partial" if processed > 0 else "failed")
        _log_run(
            started_at, datetime.now(timezone.utc), status, processed, failed,
            error_summary="; ".join(failures[:10]) if failures else None,
            details={"user_id": user_id, "universe_size": len(tickers)},
        )
        return {
            "job": JOB_NAME, "tickers_total": len(tickers),
            "tickers_processed": processed, "tickers_failed": failed,
            "status": status,
        }

    except Exception as e:
        # Total crash before/during the loop — still log a failed row
        # with whatever was captured, rather than no row at all.
        print(f"[AfterHoursBatch] Run failed entirely: {e}")
        _log_run(
            started_at, datetime.now(timezone.utc), "failed", processed, failed,
            error_summary=str(e),
            details={"user_id": user_id, "universe_size": len(tickers)},
        )
        return {
            "job": JOB_NAME, "tickers_total": len(tickers),
            "tickers_processed": processed, "tickers_failed": failed,
            "status": "failed", "error": str(e),
        }


def _snapshot_one_ticker(ticker: str, from_date: str, to_date: str, vix: float) -> None:
    from sqlalchemy import text
    from app.db.session import get_session
    from app.market_data.uw_market_data import get_bars
    from app.technical_analysis.engine import get_technical_profile
    from app.recommendations.fundamentals import get_fundamentals
    from app.signals.edgar_insider import get_insider_activity
    from app.rag.context_builder import _build_iv_context

    # ── TA ───────────────────────────────────────────────────────────────
    bars = get_bars(ticker, 1, "day", from_date, to_date)
    ta   = get_technical_profile(ticker, bars) if bars else {}

    # ── Fundamentals ─────────────────────────────────────────────────────
    fund = get_fundamentals(ticker)

    # ── Insider ──────────────────────────────────────────────────────────
    insider = get_insider_activity(ticker)

    # ── IV — existing table, existing reader; just make sure every
    # watchlist ticker gets a row today, not only enriched ones. UW calls
    # inside _build_iv_context already go through the shared rate limiter
    # (unusual_whales.py's internal _get() calls acquire_uw_token() for
    # every request), so no extra throttling needed here.
    try:
        _build_iv_context(ticker, vix=vix)
    except Exception as e:
        print(f"[AfterHoursBatch] {ticker} IV record failed: {e}")

    # ── Upsert ticker_daily_snapshot ─────────────────────────────────────
    with get_session() as s:
        s.execute(text("""
            INSERT INTO ticker_daily_snapshot (
                ticker, date,
                ma20, ma50, ma200, ema20, rsi14, macd_signal, trend,
                analyst_target_mean, analyst_count, peg_ratio,
                profit_margins, revenue_growth, analyst_recommendation,
                insider_signal, insider_csuite_buy, insider_csuite_sell,
                insider_buy_value
            ) VALUES (
                :ticker, CURRENT_DATE,
                :ma20, :ma50, :ma200, :ema20, :rsi14, :macd_signal, :trend,
                :target_mean, :analyst_count, :peg_ratio,
                :margins, :rev_growth, :analyst_rec,
                :insider_signal, :csuite_buy, :csuite_sell, :insider_buy_value
            )
            ON CONFLICT (ticker, date) DO UPDATE SET
                recorded_at            = now(),
                ma20                   = EXCLUDED.ma20,
                ma50                   = EXCLUDED.ma50,
                ma200                  = EXCLUDED.ma200,
                ema20                  = EXCLUDED.ema20,
                rsi14                  = EXCLUDED.rsi14,
                macd_signal            = EXCLUDED.macd_signal,
                trend                  = EXCLUDED.trend,
                analyst_target_mean    = EXCLUDED.analyst_target_mean,
                analyst_count          = EXCLUDED.analyst_count,
                peg_ratio              = EXCLUDED.peg_ratio,
                profit_margins         = EXCLUDED.profit_margins,
                revenue_growth         = EXCLUDED.revenue_growth,
                analyst_recommendation = EXCLUDED.analyst_recommendation,
                insider_signal         = EXCLUDED.insider_signal,
                insider_csuite_buy     = EXCLUDED.insider_csuite_buy,
                insider_csuite_sell    = EXCLUDED.insider_csuite_sell,
                insider_buy_value      = EXCLUDED.insider_buy_value
        """), {
            "ticker":         ticker.upper(),
            "ma20":           ta.get("ma20"),
            "ma50":           ta.get("ma50"),
            "ma200":          ta.get("ma200"),
            "ema20":          ta.get("ema20"),
            "rsi14":          ta.get("rsi_14"),
            "macd_signal":    ta.get("macd_signal"),
            "trend":          ta.get("trend"),
            "target_mean":    fund.get("target_mean_price"),
            "analyst_count":  fund.get("analyst_count"),
            "peg_ratio":      fund.get("peg_ratio"),
            "margins":        fund.get("profit_margins"),
            "rev_growth":     fund.get("revenue_growth"),
            "analyst_rec":    fund.get("analyst_recommendation"),
            "insider_signal": insider.get("signal"),
            "csuite_buy":     insider.get("csuite_buy"),
            "csuite_sell":    insider.get("csuite_sell"),
            "insider_buy_value": insider.get("buy_value"),
        })


def _log_run(started_at, completed_at, status: str, processed: int, failed: int,
             error_summary: str | None, details: dict) -> None:
    try:
        import json
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
                "job_name": JOB_NAME, "started_at": started_at, "completed_at": completed_at,
                "status": status, "processed": processed, "failed": failed,
                "error_summary": error_summary, "details": json.dumps(details or {}),
            })
    except Exception as e:
        print(f"[AfterHoursBatch] job_run_log write failed: {e}")
