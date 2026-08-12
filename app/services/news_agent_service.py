"""
News Agent — standalone service (MULTIAGENT_MIGRATION.md Phase B.1,
item 34).

Split out of app/api/main.py's APScheduler - same two fire times, same
single global run (no per-user loop - global market news/macro calendar
are the same for everyone). Calls run_news_agent() (app/agents/
news_agent.py) unchanged, persists to news_agent_snapshot instead of
returning in-memory.

Scope note: app/agents/news_agent.py's build_global_news()/
build_macro_context() themselves are NOT part of this split - the old
(still-live) recommendation engine (context_builder.py, smart_engine.py,
rescan_engine.py) calls those two functions directly, synchronously, on
every user-facing scan/recommendation request, and needs fresh news at
request time, not this service's twice-daily snapshot. Only the
scheduled run_news_agent() wrapper moves here; those three call sites
are untouched and keep importing app.agents.news_agent directly.

Run standalone: python3 -m app.services.news_agent_service
"""
import json
import time


def _run(trigger: str) -> None:
    from sqlalchemy import text
    from app.db.session import get_session
    from app.agents.news_agent import run_news_agent

    try:
        result = run_news_agent(trigger=trigger)
        with get_session() as s:
            s.execute(text("""
                INSERT INTO news_agent_snapshot (trigger, news, macro)
                VALUES (:trigger, CAST(:news AS jsonb), CAST(:macro AS jsonb))
            """), {
                "trigger": trigger,
                "news": json.dumps(result["news"], default=str),
                "macro": json.dumps(result["macro"], default=str),
            })
        print(f"[NewsAgentService] {trigger}: {len(result['news'])} headlines, "
              f"{result['macro'].get('high_impact_count', 0)} high-impact events")
    except Exception as e:
        print(f"[NewsAgentService] {trigger} failed: {e}")


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
        id="news_agent_post_close", replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run("pre_open"),
        CronTrigger(day_of_week="mon-fri", hour=6, minute=0, timezone=pt),
        id="news_agent_pre_open", replace_existing=True,
    )
    scheduler.start()
    print("[NewsAgentService] Started — post_close@4:15PM ET, pre_open@6:00AM PT (weekdays)")

    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
