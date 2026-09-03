"""
END-TO-END TEST: the multi-agent options-prediction pipeline.
Search Agent -> News Agent -> Prediction Agent (routing/enqueue) ->
Bull Agent + Bear Agent (debate) -> reports what Finalize WOULD do.

Deliberately stops short of calling finalize_pending_debates(): that step
opens real paper-trade positions and counts against the shared 20/day
DAILY_PICK_CAP, which is the right behavior for a human-triggered
operational run (see `bash runbook.sh pipeline`) but wrong for a test
script meant to be safely re-runnable. This test instead reports the
routing/debate outcome directly, the same way `finalize` would read it,
without executing the open-position side effect.

Uses trigger="smoketest" so it never collides with real post_close/
pre_open/manual runs' search_agent_snapshot/news_agent_snapshot rows.

Run from trading-platform root: python3 test_full_system.py
"""
import time

TRIGGER = "smoketest"
t_start = time.time()


def elapsed():
    return round(time.time() - t_start, 1)


print(f"[{elapsed()}s] Starting multi-agent pipeline smoke test (trigger={TRIGGER!r})...\n")

# ── Step 1: Search Agent ────────────────────────────────────────────────────
print(f"[{elapsed()}s] STEP 1: Search Agent")
from app.services.search_agent_service import _run as search_run
try:
    search_run(TRIGGER)
except Exception as e:
    print(f"  ❌ Search Agent failed: {e}")
    raise SystemExit(1)

from sqlalchemy import text
from app.db.session import get_session
from app.utils.current_user import get_current_user_id

admin_uid = get_current_user_id()
with get_session() as s:
    search_row = s.execute(text("""
        SELECT enriched_candidates FROM search_agent_snapshot
        WHERE user_id = :uid AND scan_date = CURRENT_DATE AND trigger = :trigger
        ORDER BY created_at DESC LIMIT 1
    """), {"uid": admin_uid, "trigger": TRIGGER}).fetchone()

n_candidates = len(search_row.enriched_candidates) if search_row and search_row.enriched_candidates else 0
print(f"  ✅ {n_candidates} enriched candidates written to search_agent_snapshot")

# ── Step 2: News Agent ──────────────────────────────────────────────────────
print(f"\n[{elapsed()}s] STEP 2: News Agent")
from app.services.news_agent_service import _run as news_run
try:
    news_run(TRIGGER)
    print("  ✅ news_agent_snapshot written")
except Exception as e:
    print(f"  ❌ News Agent failed: {e}")

# ── Step 3: Prediction Agent — enqueue_routed ───────────────────────────────
print(f"\n[{elapsed()}s] STEP 3: Prediction Agent — routing + enqueue")
t0 = time.time()
from app.services.prediction_agent_service import _enqueue_routed
_enqueue_routed(TRIGGER)

with get_session() as s:
    new_debates = s.execute(text("""
        SELECT id, ticker, agent, status FROM debate_requests
        WHERE user_id = :uid AND created_at >= to_timestamp(:t0)
        ORDER BY created_at
    """), {"uid": admin_uid, "t0": t0}).fetchall()

print(f"  ✅ {len(new_debates)} debate_requests enqueued from this run's candidates")

# ── Step 4 + 5: Bull Agent + Bear Agent ─────────────────────────────────────
print(f"\n[{elapsed()}s] STEP 4: Bull Agent")
from app.services.bull_agent_service import _run as bull_run
bull_run()

print(f"\n[{elapsed()}s] STEP 5: Bear Agent")
from app.services.bear_agent_service import _run as bear_run
bear_run()

# ── Step 6: report what Finalize would do (no real finalize call) ──────────
print(f"\n[{elapsed()}s] STEP 6: Debate outcome (finalize NOT executed — see docstring)")
ids = [row.id for row in new_debates]
if ids:
    with get_session() as s:
        settled = s.execute(text("""
            SELECT ticker, agent, status, result
            FROM debate_requests WHERE id = ANY(:ids) ORDER BY ticker
        """), {"ids": ids}).fetchall()

    by_status = {}
    for row in settled:
        by_status.setdefault(row.status, []).append(row.ticker)

    for status, tickers in sorted(by_status.items()):
        print(f"  {status:10} ({len(tickers)}): {', '.join(sorted(set(tickers)))}")

    ready = [r for r in settled if r.status == "answered"]
    print(f"\n  {len(ready)}/{len(settled)} debate_requests answered — these would be "
          f"candidates for finalize_pending_debates() to open (subject to R/R/EV gates "
          f"and the daily pick cap, not evaluated here).")
else:
    print("  No debate_requests were enqueued this run (no candidate cleared BULL_ONLY/"
          "BEAR_ONLY routing, or the daily cap was already reached) — nothing to check.")

print(f"\n{'='*50}")
print(f"TOTAL TIME: {elapsed()}s")
print("STATUS:     ✅ pipeline ran end-to-end (Search/News/Routing/Bull/Bear)"
      if n_candidates else "STATUS:     ⚠️  Search Agent returned 0 candidates")
