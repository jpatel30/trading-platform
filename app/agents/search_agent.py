"""
Search Agent (MULTIAGENT_MIGRATION.md Phase A, items 1-2).

Consolidates the three pieces of "gather today's signal universe" that
previously had no single home:
  - scanner/universe.py::get_scan_universe    (which tickers)
  - signals/after_hours_batch.py              (persisted daily
                                                TA/fundamentals/insider/
                                                IV snapshot per ticker)
  - scanner/quick_scan.py::quick_scan         (fast 6-signal convergence
                                                score per ticker)

This is an orchestration layer, not a rewrite - all three underlying
functions are proven, already have their own callers elsewhere in the
codebase (smart_engine.py/rescan_engine.py call quick_scan directly for
a live scan; the scheduler calls run_after_hours_batch directly today),
and are left exactly as they are. run_search_agent() is the ONE new
entry point everything else (the scheduler, and later the Prediction
Agent) should call to get a full pass, per Phase A's stated goal: prove
the contract in-process before paying for real service separation.

Not yet decided / deliberately out of scope for this pass: whether
quick_scan's scored candidate list gets persisted anywhere for the
Prediction Agent to consume asynchronously, or stays an ephemeral
return value used only by same-process, synchronous callers. No table
for it is specified in MULTIAGENT_MIGRATION.md (item 8's
candidate_directions table is Prediction Agent output, a later step,
not this). Left as a return value only until that's decided.
"""
import time


def run_search_agent(user_id: str, top_n: int = 15, trigger: str = "manual") -> dict:
    """
    Full Search Agent pass for one user: resolve the scan universe,
    persist today's daily snapshot (TA/fundamentals/insider/IV via
    after_hours_batch), and return the fast convergence-scored
    candidate list (quick_scan).

    trigger: informational only ("post_close" / "pre_open" / "manual")
    - identifies which scheduled slot called this in logs, does not
    change behavior. Per MULTIAGENT_MIGRATION.md item 2, "pre_open" and
    "post_close" both run this exact same full computation - the
    second trigger's whole point is catching overnight movement with
    the SAME pass, not a different/lighter one.
    """
    from app.scanner.universe import get_scan_universe
    from app.scanner.quick_scan import quick_scan
    from app.signals.after_hours_batch import run_after_hours_batch

    t0 = time.time()
    print(f"[SearchAgent] Starting ({trigger}) for user {user_id[:8]}...")

    tickers = get_scan_universe(user_id, watchlist_mode="default_plus_mine")

    # run_after_hours_batch resolves its own universe internally (same
    # function, unchanged) - the small re-resolution here is a cheap,
    # local DB lookup, not worth threading a ticker list through its
    # signature just to save one query and add risk to a working job.
    batch_result = run_after_hours_batch(user_id)

    scored = quick_scan(tickers, user_id=user_id, top_n=top_n) if tickers else []

    elapsed = round(time.time() - t0, 1)
    print(f"[SearchAgent] Done ({trigger}) in {elapsed}s — "
          f"{len(tickers)} tickers, batch={batch_result.get('status')}, "
          f"{len(scored)} candidates")

    return {
        "agent": "search",
        "trigger": trigger,
        "user_id": user_id,
        "universe_size": len(tickers),
        "batch": batch_result,
        "candidates": scored,
        "elapsed": elapsed,
    }
