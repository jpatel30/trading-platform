"""
Bear Agent — standalone service (MULTIAGENT_MIGRATION.md Phase B.2,
item 37).

Mirrors bull_agent_service.py exactly, "bear" instead of "bull" - see
that file's docstring for the full rationale (no per-user loop, reads/
writes only debate_requests, same three-slot schedule).

Run standalone: python3 -m app.services.bear_agent_service
"""
import time


def _run() -> None:
    from app.agents.bull_bear_agents import process_pending_debate_requests
    result = process_pending_debate_requests("bear")
    print(f"[BearAgentService] {result}")


def main() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    et = pytz.timezone("America/New_York")
    pt = pytz.timezone("America/Los_Angeles")
    scheduler = BackgroundScheduler(timezone=et)

    scheduler.add_job(
        _run, CronTrigger(day_of_week="mon-fri", hour=16, minute=35, timezone=et),
        id="bear_agent_post_close", replace_existing=True,
    )
    scheduler.add_job(
        _run, CronTrigger(day_of_week="mon-fri", hour=6, minute=20, timezone=pt),
        id="bear_agent_pre_open", replace_existing=True,
    )
    scheduler.add_job(
        _run, CronTrigger(day_of_week="mon-fri", hour=8, minute=40, timezone=pt),
        id="bear_agent_tie_break", replace_existing=True,
    )
    scheduler.start()
    print("[BearAgentService] Started — 4:35PM ET, 6:20AM PT, 8:40AM PT (weekdays)")

    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
