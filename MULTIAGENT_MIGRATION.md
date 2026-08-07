# Multi-Agent Migration — Task List

This tracks the migration from the current single-process recommendation
engine to the 6-agent architecture (Search / News / Prediction / Bull /
Bear / Learning). Separate from REMAINING_ITEMS.md, which tracks smaller
fixes to the current system — this is the architecture change itself.

Phased deliberately: extract as modules first (same process, cheap to
validate the data contracts between agents) before paying for real
service separation. Real services are the explicit end goal (confirmed:
each agent should be independently usable), just sequenced after the
contracts are proven, not before.

---

## Phase A — Extract as modules, prove the data contracts

### Search Agent
1. Consolidate after_hours_batch.py + quick_scan.py's enrichment step +
   scanner/universe.py into one module with a single entry point.
2. Add the second daily trigger — 6am PT pre-open, same full computation
   as the existing post-close run (catches overnight movement).
3. Verify get_congress_trades()'s actual current endpoint against
   UW's enterprise politician-portfolios/recent_trades endpoint —
   confirm it's already the same one, or flag if an upgrade is needed.
4. Build the transcript/filing fetch + chunk + embed pipeline (new —
   nothing like this exists yet). Incremental: check for and embed only
   NEW documents since the last run, not a blind full re-embed.

### News Agent
5. Extract _build_global_news() + the economic-calendar wiring out of
   context_builder.py into its own standalone module.
6. Add the same second daily trigger (6am PT, overnight news check).

### Prediction Agent — Routing Phase (new)
7. Build the deterministic, pre-LLM pass: for every ticker, use the
   existing signal/math layers (flow_scoring, oi_flow, market_regime,
   iv_expansion, technical_analysis, the R/R and EV gates) to compute a
   direction lean, rough strategy shape, and rough strikes.
8. New table: candidate_directions (ticker, strategy shape, rough
   strikes, direction lean, confidence-of-routing).
9. New table: tie_break_queue, including a which_rule_conflict_triggered
   field (which specific signals disagreed, not just "it was ambiguous").
10. Classification logic: clear bullish shape → Bull only; clear bearish
    shape → Bear only; genuinely mixed signals → tie_break_queue.

### Bull Agent / Bear Agent (new — today this is one LLM call, not two)
11. Build Bull Agent: a focused prompt arguing only the bullish case for
    a routed ticker + strategy shape.
12. Build Bear Agent: mirror, bearish case only.
13. Both must support two invocation modes: routed (single-agent, main
    pass) and tie-break (both agents, same ticker, after the main pass
    fully completes).

### Retrieval Library (new, shared code — not a service)
14. Similar-outcome lookup: plain SQL over paper_trade_context +
    daily_recommendations, matched on the same categorical buckets
    Phase 6's weekly review already computes. No embeddings.
15. Semantic search: query-time embed + search against the corpus
    Search Agent builds (#4). Bull/Bear only ever query an existing
    corpus, never build one live.
16. Wire directly into Bull and Bear as imported code — confirmed this
    stays a shared library, not exposed as its own service.
17. Actually wire up ChromaDB for the first time — this finally answers
    the long-open "remove or use it" question with a real use.

### Prediction Agent — Finalize Phase
18. Extract strategy/engine.py's existing R/R gate, EV gate, and
    structural-impossibility backstop into a module that explicitly
    takes Bull + Bear output (rather than a single LLM's output, as
    today) and re-verifies before storing.
19. Must handle both the main pass and the tie-break pass, tagging
    which_rule_conflict_triggered on tie-break results specifically.

### Tie-break sequencing
20. Queue accumulates during routing (#7-10), held back from the main
    pass entirely.
21. Tie-break batch runs ONLY after the main pass fully completes — not
    concurrently. This is a direct, deliberate consequence of the
    enrichment-timeout bug found earlier this session (concurrent
    threads competing for the same rate-limited resources), not an
    arbitrary sequencing choice.
22. Resolved tie-break picks open at whichever of the existing intraday
    windows (8:30 / 10:30 / 12:30 PT) comes next — no new time slot
    needed, same shared ~20/day cap applies.

### Learning Agent
23. Change weekly_review.py's cadence: Sunday-only → daily.
24. Change the lookback: instead of "just-completed Mon-Fri," compute a
    genuine rolling 7-day window fresh every single run.
25. Add which_rule_conflict_triggered as a new bucket dimension
    alongside the existing ones (strategy rule, OI persistence, IV
    trend, intraday timeframes, window length, conviction tier).
26. Build the actual task_list.md-writing output — today's version only
    writes structured stats + an optional LLM-phrased summary; this
    needs to translate findings into concrete, actionable suggestions
    for Search / News / Prediction / Bull / Bear specifically.

### Broker removal (confirmed: no account linking for anyone, including
the former admin)
27. Remove webull_connector.py and every BrokerNotConnectedError
    handling path built around it.
28. Remove the admin-only portfolio strip (layout.tsx) and the
    dedicated portfolio/page.tsx route entirely.
29. Remove check_fills()'s Webull-position auto-detection logic
    entirely — there's no broker left to check against.
30. Confirm is_admin's remaining purpose (shared default watchlist
    ownership, the all-users history view) still works correctly once
    every broker-related power is stripped from it — it doesn't go
    away, just loses its "has a live broker" meaning.
31. Expand the existing Fill/confirm_execution flow to be the ONLY way
    any user — including the former admin — reports a real fill, since
    broker auto-detection no longer exists for anyone.

### Confirmed correct, no work needed
32. Watchlist default (admin's list = shared default when a user hasn't
    added their own) — already the existing, correct mechanism. Listed
    here explicitly so it isn't mistaken for a missed task.

---

## Phase B — Split into real, separate services

Only once Phase A's data contracts (the tables above) are stable and
proven. Each of these is genuinely independent once split — the whole
point of choosing real services over modules.

33. Search Agent → own service.
34. News Agent → own service.
35. Prediction Agent (both phases) → own service.
36. Bull Agent → own service.
37. Bear Agent → own service.
38. Learning Agent → own service.
39. Confirm the ONLY inter-service communication is through the shared
    Postgres tables above — no direct function calls or imports across
    a service boundary (the Retrieval Library is the deliberate
    exception, since it's not a separate service in the first place).

---

## Phase C — Paper-to-live transition

Deferred until the validation period ends, but worth having a real plan
now rather than figuring it out later under pressure.

40. Decide and define the actual trigger for ending the paper-trading
    validation period (a time window, a confidence threshold from
    Learning Agent's stats, or an explicit manual decision).
41. Turn off the automated paper-trade-open/close jobs once live.
42. Confirm the Fill/confirm_execution flow (already built for #31)
    becomes the real, primary way every enrolled user logs a real fill
    going forward — same mechanism, same tables, just no longer tagged
    source='auto_paper'.
