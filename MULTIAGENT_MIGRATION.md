# Multi-Agent Migration — Task List

This tracks the migration from the current single-process recommendation
engine to the 6-agent architecture (Search / News / Prediction / Bull /
Bear / Learning). Separate from REMAINING_ITEMS.md, which tracks smaller
fixes to the current system — this is the architecture change itself.

Audited against live code + the live Postgres/ChromaDB state on 2026-08-13.
27 of the original 42 tasks were verified DONE and removed from this file.
The cutover gap found in that audit was fixed, verified live, and shipped
on 2026-09-02 (`adec414`) — see git history for detail. What's left below
is genuinely open.

All agent LLM calls (Bull/Bear debates, Prediction routing narrative,
Learning weekly review) run on a local Ollama model, currently
`qwen2.5:14b` — set via `OLLAMA_MODEL` in `.env`, default in
`app/utils/config.py`. Embeddings (filing chunks, retrieval library) use
`nomic-embed-text`, same host. No hosted/API LLM in the pipeline today.

---

## Phase A — remaining

4. Transcript fetch+chunk+embed still not built — blocked on UW's
   enterprise tier (confirmed via live 403 on the transcripts endpoint).
   The filing (8-K) half of this pipeline is done and verified live
   (incremental-check logic + 6 real embedded chunks in ChromaDB).

2/6. The 6am PT pre-open triggers for Search Agent and News Agent are
   coded correctly and now on a real schedule (cutover above), but still
   unverified end-to-end with a real trading day's output — 0 rows ever
   in `search_agent_snapshot`/`news_agent_snapshot` as of last audit.
   Re-check once a weekday run has actually fired.

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
    jobs. Not urgent while still in paper phase (unchanged since the
    2026-08-13 audit).

42. `confirm_execution` still hardcodes `source='auto_paper'` at every
    open site (`prediction_agent.py`, `paper_trading.py`). Not yet the
    sole primary live-fill mechanism — correctly deferred until #40/#41
    are resolved.
