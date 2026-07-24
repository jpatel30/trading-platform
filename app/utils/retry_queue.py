"""
Shared best-effort-then-retry-once pattern.

Used by after_hours_batch's per-ticker loop, paper_trade_open's
per-combo pool, and paper_trade_close's per-position loop — one utility
instead of three separately-built retry loops, each of which would
otherwise reinvent the same "run the normal pass, collect exactly what
failed, wait a short buffer, retry once, log clearly whatever still
fails" shape.
"""
import time


def run_with_retry(items: list, worker_fn, retry_delay_seconds: int = 45, label: str = "item") -> dict:
    """
    Runs worker_fn(item) once for every item (the normal best-effort
    pass). Collects exactly which items failed, waits retry_delay_seconds
    (avoids hammering an API that may have just rate-limited the first
    pass), then retries ONLY the failed items once. Whatever still fails
    after that is logged clearly as a genuine, persistent failure — no
    infinite retry loop.

    worker_fn(item) -> dict, and the dict MUST include an "ok": bool key
    (ok=True means this attempt succeeded, ok=False means it failed and
    should be retried). Any other keys are caller-defined and preserved
    as-is. If worker_fn raises instead of returning, the exception is
    caught here and treated as ok=False with the exception message
    under "error" — callers don't need their own try/except purely to
    satisfy this contract.

    Returns:
        {
            "results": [dict, ...],   # same order/length as items, each
                                       # tagged with "attempt": "first" or "retry"
            "succeeded_first_pass": [int indices into items],
            "succeeded_on_retry":   [int indices into items],
            "failed_both_passes":   [int indices into items],
        }
    """
    results: list = [None] * len(items)
    failed_indices: list[int] = []

    for i, item in enumerate(items):
        try:
            r = worker_fn(item)
        except Exception as e:
            r = {"ok": False, "error": str(e)}
        r = dict(r)
        r["attempt"] = "first"
        results[i] = r
        if not r.get("ok"):
            failed_indices.append(i)

    succeeded_first_pass = [i for i in range(len(items)) if i not in failed_indices]
    succeeded_on_retry: list[int] = []
    failed_both_passes: list[int] = []

    if failed_indices:
        print(f"[RetryQueue] {len(failed_indices)}/{len(items)} {label}(s) failed the first pass — "
              f"waiting {retry_delay_seconds}s before retrying only those")
        time.sleep(retry_delay_seconds)
        for i in failed_indices:
            try:
                r = worker_fn(items[i])
            except Exception as e:
                r = {"ok": False, "error": str(e)}
            r = dict(r)
            r["attempt"] = "retry"
            results[i] = r
            if r.get("ok"):
                succeeded_on_retry.append(i)
            else:
                failed_both_passes.append(i)
        if failed_both_passes:
            print(f"[RetryQueue] {len(failed_both_passes)}/{len(failed_indices)} {label}(s) still "
                  f"failed after the retry — giving up (no infinite loop)")

    return {
        "results": results,
        "succeeded_first_pass": succeeded_first_pass,
        "succeeded_on_retry": succeeded_on_retry,
        "failed_both_passes": failed_both_passes,
    }
