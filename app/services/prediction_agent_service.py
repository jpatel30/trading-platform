"""
Prediction Agent — standalone service (MULTIAGENT_MIGRATION.md Phase
B.2, item 35 — "both phases" = routing/enqueue AND finalize, both live
in this one service).

Bull Agent and Bear Agent are separate processes now (bull_agent_service.py,
bear_agent_service.py) - this service can no longer call
run_bull_bear_debate() directly and get a result back in the same
function call the way main.py's old _run_main_pass did. It writes
debate_requests rows instead (enqueue_routed_candidates/
enqueue_tie_break_candidates, app/agents/prediction_agent.py) and, on a
later scheduled run, reads back whatever Bull/Bear answered
(finalize_pending_debates) - the entire inter-service contract is that
one shared table (item 39).

Admin-only, same single-platform-signal-generation principle every
other paper-trading job in this codebase follows (this is what OPENS
real positions) - mirrors app/api/main.py's _get_paper_trade_admin_user_id
exactly, duplicated here in miniature since that helper is a closure
nested inside main.py's startup_event and isn't importable.

Schedule (10-minute buffers, same pattern Phase B.1 established between
Search/News and their downstream consumer):
    16:25 ET / 06:10 PT  enqueue_routed   (10 min after Search/News fire)
    16:35 ET / 06:20 PT  Bull/Bear check  (owned by their own services)
    16:45 ET / 06:30 PT  finalize
    08:30 PT             enqueue_tie_break (same slot the old
                          tie_break_batch_0830 job used)
    08:40 PT             Bull/Bear check  (owned by their own services)
    08:50 PT             finalize

Run standalone: python3 -m app.services.prediction_agent_service
"""
import time


def _get_admin_user_id() -> str | None:
    from sqlalchemy import text
    from app.db.session import get_session
    with get_session() as s:
        row = s.execute(text(
            "SELECT id FROM users WHERE is_admin=TRUE AND is_active=TRUE LIMIT 1"
        )).fetchone()
    return str(row.id) if row else None


def _enqueue_routed(trigger: str) -> None:
    from sqlalchemy import text
    from app.db.session import get_session
    from app.agents.prediction_agent import enqueue_routed_candidates
    from app.rag.context_builder import _build_vix_context
    from app.signals.market_regime import get_full_market_regime

    admin_uid = _get_admin_user_id()
    if not admin_uid:
        print(f"[PredictionAgentService] enqueue_routed ({trigger}) skipped: no admin user found")
        return

    with get_session() as s:
        search_row = s.execute(text("""
            SELECT enriched_candidates FROM search_agent_snapshot
            WHERE user_id = :uid AND scan_date = CURRENT_DATE AND trigger = :trigger
            ORDER BY created_at DESC LIMIT 1
        """), {"uid": admin_uid, "trigger": trigger}).fetchone()
        news_row = s.execute(text("""
            SELECT macro FROM news_agent_snapshot
            WHERE snapshot_date = CURRENT_DATE AND trigger = :trigger
            ORDER BY created_at DESC LIMIT 1
        """), {"trigger": trigger}).fetchone()

    if not search_row or not search_row.enriched_candidates:
        print(f"[PredictionAgentService] enqueue_routed ({trigger}) skipped: "
              f"no search_agent_snapshot for today yet")
        return

    enriched = search_row.enriched_candidates
    macro    = news_row.macro if news_row else {}

    regime = get_full_market_regime()
    vix    = _build_vix_context()
    market_context = {"vix": vix, "regime": regime, "econ_events": macro.get("upcoming_events", "no data")}

    result = enqueue_routed_candidates(admin_uid, enriched, market_regime=regime, market_context=market_context)
    print(f"[PredictionAgentService] enqueue_routed ({trigger}): {result}")


def _enqueue_tie_break() -> None:
    from app.agents.prediction_agent import enqueue_tie_break_candidates

    admin_uid = _get_admin_user_id()
    if not admin_uid:
        print("[PredictionAgentService] enqueue_tie_break skipped: no admin user found")
        return

    result = enqueue_tie_break_candidates(admin_uid)
    print(f"[PredictionAgentService] enqueue_tie_break: {result}")


def _finalize(label: str) -> None:
    from app.agents.prediction_agent import finalize_pending_debates

    admin_uid = _get_admin_user_id()
    if not admin_uid:
        print(f"[PredictionAgentService] finalize ({label}) skipped: no admin user found")
        return

    result = finalize_pending_debates(admin_uid)
    print(f"[PredictionAgentService] finalize ({label}): {result}")


def main() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    et = pytz.timezone("America/New_York")
    pt = pytz.timezone("America/Los_Angeles")
    scheduler = BackgroundScheduler(timezone=et)

    scheduler.add_job(
        lambda: _enqueue_routed("post_close"),
        CronTrigger(day_of_week="mon-fri", hour=16, minute=25, timezone=et),
        id="prediction_enqueue_routed_post_close", replace_existing=True,
    )
    scheduler.add_job(
        lambda: _enqueue_routed("pre_open"),
        CronTrigger(day_of_week="mon-fri", hour=6, minute=10, timezone=pt),
        id="prediction_enqueue_routed_pre_open", replace_existing=True,
    )
    scheduler.add_job(
        lambda: _finalize("post_close"),
        CronTrigger(day_of_week="mon-fri", hour=16, minute=45, timezone=et),
        id="prediction_finalize_post_close", replace_existing=True,
    )
    scheduler.add_job(
        lambda: _finalize("pre_open"),
        CronTrigger(day_of_week="mon-fri", hour=6, minute=30, timezone=pt),
        id="prediction_finalize_pre_open", replace_existing=True,
    )
    scheduler.add_job(
        _enqueue_tie_break,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=pt),
        id="prediction_enqueue_tie_break", replace_existing=True,
    )
    scheduler.add_job(
        lambda: _finalize("tie_break"),
        CronTrigger(day_of_week="mon-fri", hour=8, minute=50, timezone=pt),
        id="prediction_finalize_tie_break", replace_existing=True,
    )
    scheduler.start()
    print("[PredictionAgentService] Started — "
          "enqueue_routed@4:25PM ET,6:10AM PT | finalize@4:45PM ET,6:30AM PT | "
          "enqueue_tie_break@8:30AM PT | finalize@8:50AM PT (weekdays)")

    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
