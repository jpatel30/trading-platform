"""
Bull Agent — standalone service (MULTIAGENT_MIGRATION.md Phase B.2,
item 36).

No per-user loop, no admin scoping - unlike Prediction Agent, Bull Agent
doesn't decide WHO to generate signals for, it just answers whatever
debate_requests rows Prediction Agent already enqueued (bull_bear_agents.py::
process_pending_debate_requests, unchanged core logic, same run_bull_agent()
LLM call as before). Reading/writing that one shared table is the entire
inter-service contract (item 39) - this file never imports anything from
app/agents/prediction_agent.py.

Schedule: checks 10 minutes after each of Prediction Agent's three
enqueue slots (see prediction_agent_service.py), 10 minutes before the
matching finalize slot, so there's a real window to answer before
Finalize looks and (per the confirmed policy) times out anything still
pending.

Run standalone: python3 -m app.services.bull_agent_service
"""
import time


def _run() -> None:
    from app.agents.bull_bear_agents import process_pending_debate_requests
    result = process_pending_debate_requests("bull")
    print(f"[BullAgentService] {result}")


def main() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    et = pytz.timezone("America/New_York")
    pt = pytz.timezone("America/Los_Angeles")
    scheduler = BackgroundScheduler(timezone=et)

    scheduler.add_job(
        _run, CronTrigger(day_of_week="mon-fri", hour=16, minute=35, timezone=et),
        id="bull_agent_post_close", replace_existing=True,
    )
    scheduler.add_job(
        _run, CronTrigger(day_of_week="mon-fri", hour=6, minute=20, timezone=pt),
        id="bull_agent_pre_open", replace_existing=True,
    )
    scheduler.add_job(
        _run, CronTrigger(day_of_week="mon-fri", hour=8, minute=40, timezone=pt),
        id="bull_agent_tie_break", replace_existing=True,
    )
    scheduler.start()
    print("[BullAgentService] Started — 4:35PM ET, 6:20AM PT, 8:40AM PT (weekdays)")

    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()
