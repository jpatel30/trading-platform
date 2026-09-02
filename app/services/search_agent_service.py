"""
Search Agent — standalone service (MULTIAGENT_MIGRATION.md Phase B.1,
item 33).

Split out of app/api/main.py's APScheduler - same two fire times, same
per-user loop, same run_search_agent()/enrich_candidates() calls
(app/agents/search_agent.py, unchanged). The only real change from the
in-process version: results are now persisted to search_agent_snapshot
instead of returned in-memory, since Prediction Agent (still living in
main.py until Phase B.2) runs in a different process now and can only
see this service's output through Postgres, never a Python return value
or a direct import of this file.

enrich_candidates() runs here too, not in main.py - enrichment is Search
Agent turning a raw scored ticker into something usable, so the snapshot
this service writes is the full, ready-to-route candidate list; whatever
reads search_agent_snapshot later never needs to know enrich_candidates
exists.

Run standalone: python3 -m app.services.search_agent_service
"""
import json
import time


def _run(trigger: str) -> None:
    from sqlalchemy import text
    from app.db.session import get_session
    from app.agents.search_agent import run_search_agent, enrich_candidates

    t0 = time.time()
    with get_session() as s:
        users = s.execute(text("SELECT id FROM users WHERE is_active=TRUE")).fetchall()

    for u in users:
        uid = str(u.id)
        try:
            result   = run_search_agent(uid, trigger=trigger)
            enriched = enrich_candidates(result["candidates"], uid)
            with get_session() as s:
                s.execute(text("""
                    INSERT INTO search_agent_snapshot (
                        user_id, trigger, universe_size, batch_status, enriched_candidates
                    ) VALUES (
                        :uid, :trigger, :universe_size, :batch_status, CAST(:candidates AS jsonb)
                    )
                """), {
                    "uid": uid, "trigger": trigger,
                    "universe_size": result["universe_size"],
                    "batch_status": result["batch"].get("status"),
                    "candidates": json.dumps(enriched, default=str),
                })
            print(f"[SearchAgentService] {trigger}: {uid[:8]} — "
                  f"{result['universe_size']} tickers, {len(enriched)} enriched candidates")
        except Exception as e:
            print(f"[SearchAgentService] {trigger}: {uid[:8]} failed: {e}")

    print(f"[SearchAgentService] {trigger} pass done in {round(time.time() - t0, 1)}s")


def _run_filing_embed() -> None:
    """
    MULTIAGENT_MIGRATION.md item 4 - daily, once/day (not pre_open too:
    unlike price/news, filings don't need a same-day overnight-catch-up
    pass; the incremental ChromaDB-existence check already makes re-runs
    cheap regardless). No same-day consumer depends on this finishing by
    any particular time - Bull/Bear only ever query whatever corpus
    already exists (retrieval_library.py), never block waiting for a
    fresher one - so it's scheduled well clear of the 4:15-4:45pm ET
    routing chain rather than coordinated with it.
    """
    from app.scanner.universe import get_scan_universe
    from app.agents.filing_embed import run_filing_embed

    t0 = time.time()
    admin_uid = _get_admin_user_id()
    if not admin_uid:
        print("[SearchAgentService] filing_embed skipped: no admin user found")
        return

    tickers = get_scan_universe(user_id=admin_uid, watchlist_mode="default_only")
    result  = run_filing_embed(tickers)
    print(f"[SearchAgentService] filing_embed: {result} in {round(time.time() - t0, 1)}s")


def _get_admin_user_id() -> str | None:
    from sqlalchemy import text
    from app.db.session import get_session
    with get_session() as s:
        row = s.execute(text(
            "SELECT id FROM users WHERE is_admin=TRUE AND is_active=TRUE LIMIT 1"
        )).fetchone()
    return str(row.id) if row else None


def main() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    et = pytz.timezone("America/New_York")
    pt = pytz.timezone("America/Los_Angeles")
    scheduler = BackgroundScheduler(timezone=et)

    scheduler.add_job(
        lambda: _run("post_close"),
        CronTrigger(day_of_week="mon-fri", hour=16, minute=15, timezone=et),
        id="search_agent_post_close", replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run("pre_open"),
        # 6am PT, same slot main.py used to own - ~40 min before the
        # first paper-trade-open window (6:40am PT).
        CronTrigger(day_of_week="mon-fri", hour=6, minute=0, timezone=pt),
        id="search_agent_pre_open", replace_existing=True,
    )
    scheduler.add_job(
        _run_filing_embed,
        # Clear of the 4:15-4:45pm ET Search/News/Prediction chain -
        # nothing same-day depends on this, so no buffer coordination
        # needed with anything else.
        CronTrigger(day_of_week="mon-fri", hour=17, minute=0, timezone=et),
        id="search_agent_filing_embed", replace_existing=True,
    )
    scheduler.start()
    print("[SearchAgentService] Started — post_close@4:15PM ET, pre_open@6:00AM PT, "
          "filing_embed@5:00PM ET (weekdays)")

    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
