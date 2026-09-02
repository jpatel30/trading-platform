# Multi-Agent Migration — Task List

This tracks the migration from the current single-process recommendation
engine to the 6-agent architecture (Search / News / Prediction / Bull /
Bear / Learning). Separate from REMAINING_ITEMS.md, which tracks smaller
fixes to the current system — this is the architecture change itself.

Audited against live code + the live Postgres/ChromaDB state on 2026-08-13.
27 of the original 42 tasks were verified DONE and removed from this file.
The cutover gap found in that audit (below) was fixed and verified live
on 2026-08-14. What's left below is genuinely open.

---

## CUTOVER — done (2026-08-14)

`app/api/main.py`'s scheduler previously still called the OLD pipeline
(`_run_paper_trade_open_options` -> `rescan_engine.rescan_with_validation`
-> `smart_engine._execute_smart_rec`) 4x/day, racing the new pipeline for
the same `DAILY_PICK_CAP`/`confirm_execution` sink, while the 5 new
services had never actually run on a schedule (0 rows ever in
`search_agent_snapshot`/`news_agent_snapshot`).

Fixed: `_run_paper_trade_open_options` and its scheduler registration
removed from `main.py` (stock picks/`_run_paper_trade_open_stocks`
untouched — unrelated to this migration). The 5 services are now spawned
and supervised as separate OS processes directly by the FastAPI process
itself (`startup_event`/`shutdown_event` in `main.py`), not launchd —
launchd-spawned processes can't access this project's path under
`~/Documents` (macOS TCC/Full-Disk-Access restriction on that folder),
while this interactively-launched process already can. A 5-minute
supervisor job restarts any that die; a pre-spawn cleanup step kills
stale orphans on every startup so a `--reload`/redeploy never leaves two
sets running at once. `runbook.sh start`/`stop` now start/stop all of
it as one unit (stopping the API stops the 5 services with it).
`health_check.sh` checks real-time process liveness (`ps` pattern match)
plus same-day data freshness once each trigger's fire time has passed.

Verified live: all 5 processes running as exactly one instance each,
clean startup banners with correct schedules in
`logs/<service>_service.log`, `health_check.sh` reports ALL SYSTEMS GO.
Not yet verified: a full trading day's worth of real output (snapshots
firing at 6:00AM PT / 4:15PM ET, routing, debates, opens) — this repo's
audit ran on a weekend, so nothing was due yet.

---

## Phase A — remaining

4. Transcript fetch+chunk+embed still not built — blocked on UW's
   enterprise tier (confirmed via live 403 on the transcripts endpoint).
   The filing (8-K) half of this pipeline is done and verified live
   (incremental-check logic + 6 real embedded chunks in ChromaDB).

2/6. The 6am PT pre-open triggers for Search Agent and News Agent are
   coded correctly but unverified in production — 0 rows ever in
   `search_agent_snapshot`/`news_agent_snapshot`. Re-check once the
   cutover above puts these services on a real schedule.

26. `task_list.md` generation never populates a News Agent section —
    no bucket dimension in `weekly_review.py` maps findings back to
    News Agent specifically, so its section is structurally possible
    but always empty.

28. The admin-only portfolio strip is still live in
    `stockbros/src/app/dashboard/page.tsx` (gated by `isAdmin` around
    lines 480, 509-518, 643-660+), with a stale comment in `layout.tsx`
    still referencing Webull/`BrokerNotConnectedError` — neither exists
    on the backend anymore (item 27 removed them). The dedicated
    `portfolio/page.tsx` route itself is already gone. Remove the strip
    and the stale comment.

Cleanup (not in original numbering): `runbook.sh`'s `check_sells()` and
`monday_checklist()` still `import webull_connector` — dead code left
over from item 27's removal, will error if either function is invoked.

---

## Phase B — remaining

38. Learning Agent has no standalone service. `run_nightly_loop` and
    `run_weekly_strategy_review` are still scheduled directly inside
    `app/api/main.py`'s embedded scheduler — never split out like the
    other 5 agents were.

33-37 (partial caveat, not a full reopen): none of the 5 services are
    containerized — `docker-compose.yml` only defines `postgres` and
    `chromadb`. They're separate OS processes (communicating only
    through Postgres, per item 39) but spawned/supervised by the
    FastAPI process rather than independently deployable — coupled to
    its lifecycle rather than truly standalone. Only worth decoupling
    further if independent deploys/scaling are actually needed.

---

## Phase C — Paper-to-live transition (deferred by design, still open)

40. No defined trigger yet for ending the paper-trading validation
    period (time window, confidence threshold from Learning Agent's
    stats, or explicit manual decision) — still undecided.

41. No kill-switch exists for the automated paper-trade-open/close
    jobs. Not urgent while still in paper phase (still active today,
    2026-08-13).

42. `confirm_execution` still hardcodes `source='auto_paper'` at every
    open site (`prediction_agent.py`, `paper_trading.py`). Not yet the
    sole primary live-fill mechanism — correctly deferred until #40/#41
    are resolved.
