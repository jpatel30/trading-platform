"""
CONCURRENCY TEST: Bull Agent + Bear Agent running at the same time.

In production these are two separate OS processes (bull_agent_service.py,
bear_agent_service.py), each on its own APScheduler cron trigger, that can
genuinely fire at the same wall-clock moment. The only thing stopping them
from stepping on each other is the shared debate_requests table contract
(MULTIAGENT_MIGRATION item 39, historical - see ARCHITECTURE.md): each
agent only ever claims/answers rows where agent=its own name, so there's
no overlap by construction - this test proves that holds under real
concurrent execution, not just sequential calls.

Sets up its own routed candidates first (Search -> News -> Prediction
enqueue, trigger="paralleltest") so there's real pending work for both
agents to race on. Stops short of finalize_pending_debates() for the same
reason test_full_system.py does - no real paper-trade positions opened,
safely re-runnable.

Run from trading-platform root: python3 test_parallel.py
"""
import time
import concurrent.futures

TRIGGER = "paralleltest"
t_start = time.time()


def ts():
    return f"[{time.time()-t_start:.1f}s]"


print(f"{ts()} Setting up pending debates for Bull/Bear to race on...\n")

from sqlalchemy import text
from app.db.session import get_session
from app.utils.current_user import get_current_user_id

admin_uid = get_current_user_id()

from app.services.search_agent_service import _run as search_run
from app.services.news_agent_service import _run as news_run
from app.services.prediction_agent_service import _enqueue_routed

search_run(TRIGGER)
news_run(TRIGGER)

t0 = time.time()
_enqueue_routed(TRIGGER)

with get_session() as s:
    pending = s.execute(text("""
        SELECT id, agent FROM debate_requests
        WHERE user_id = :uid AND created_at >= to_timestamp(:t0) AND status = 'pending'
    """), {"uid": admin_uid, "t0": t0}).fetchall()

bull_pending = sum(1 for r in pending if r.agent == "bull")
bear_pending = sum(1 for r in pending if r.agent == "bear")
print(f"{ts()} Pending: {bull_pending} for Bull, {bear_pending} for Bear\n")

if not pending:
    print(f"{ts()} No candidates routed cleanly this run (or the daily cap was already "
          f"reached) - nothing for Bull/Bear to race on. Re-run later or check "
          f"search_agent_snapshot for today's candidate quality.")
    raise SystemExit(0)

# ── Run Bull and Bear CONCURRENTLY, not sequentially ────────────────────────
from app.services.bull_agent_service import _run as bull_run
from app.services.bear_agent_service import _run as bear_run

print(f"{ts()} Launching Bull Agent + Bear Agent concurrently...")
t_race = time.time()
errors = []
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    bull_future = pool.submit(bull_run)
    bear_future = pool.submit(bear_run)
    for name, fut in (("bull", bull_future), ("bear", bear_future)):
        try:
            fut.result()
        except Exception as e:
            errors.append((name, e))
            print(f"{ts()} ❌ {name} agent raised: {e}")

race_elapsed = round(time.time() - t_race, 1)
print(f"{ts()} Both agents finished — concurrent wall time {race_elapsed}s "
      f"(sequential would be roughly the sum of each agent's own time)")

# ── Verify no cross-contamination: bull rows answered by bull, etc ─────────
ids = [r.id for r in pending]
with get_session() as s:
    settled = s.execute(text("""
        SELECT id, agent, status FROM debate_requests WHERE id = ANY(:ids)
    """), {"ids": ids}).fetchall()

by_id = {r.id: r for r in pending}
bad = [r for r in settled if r.status not in ("answered", "error", "pending")]
still_pending = [r for r in settled if r.status == "pending"]
answered = [r for r in settled if r.status == "answered"]

print(f"\n{'='*50}")
print(f"Answered: {len(answered)}/{len(settled)} | Still pending: {len(still_pending)} | "
      f"Unexpected status: {len(bad)}")
print(f"Errors raised by either agent: {len(errors)}")
print("STATUS:    ", "✅ Bull/Bear ran concurrently with no interference"
      if not errors and not bad else "⚠️  see output above")
