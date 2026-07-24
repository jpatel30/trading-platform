# Remaining Items

Last updated: July 2026

Last updated: July 2026. Full technical narrative (root causes, exact fixes, line-by-line verification) for completed work lives in git log commit messages - this doc stays a scannable status/priority list, not a technical diary.

Priority Order (Remaining)
Root-cause today's scheduler double-fire bug
Build per-combo idempotency (ticker+window+budget+day)
Build shared retry-queue mechanism (batch + paper-trade jobs)
Wire iv_history into prediction (IV-expansion signal)
Expand paper-trade scheduling to multiple times/day, capped at ~20 unique picks/day total
Fix check_fills() - matches bare ticker, not real contract
Add a real login path for returning users
Add MCP key regeneration
Deploy somewhere real (Cloudflare tunnel / Railway)
Point the hosted MCP server at that deployment
Fix get_bars() wrapper silently ignoring intraday multiplier
Wire unused UW data into scoring (econ calendar, net-flow-by-expiry, congress trades, insider ownership)
Improve ETF scoring in stock scans
Fix 3m "both" horizon's stock half (still on the naive per-ticker loop)
Confirm/fix monitor_config table usage
Decide on ChromaDB (remove container, or wire into real RAG)
Delete or repurpose orphaned conviction.py
Populate tracked_positions.daily_rec_id
Revisit paper-trading calibration assumptions once real data exists
Build real Robinhood/IBKR/Tastytrade connections
Turn ad-hoc schema changes into versioned migrations
Fix the requirements.txt typo
Open-source readiness sweep
Build the two architecture diagrams
Prep interview talking points
Add WebSocket real-time dashboard updates
Add mobile push notifications
Build a public invite page
Descriptions

1. Root-cause today's scheduler double-fire bug No second launchd job matches paper_trade - rules out a duplicate external trigger. paper_trade_open_stocks fired twice today, 105s apart (06:40:00 success/20, 06:41:45 failed/"7 of 20 futures unfinished"). Likely the same in-process APScheduler fragility already known to silently miss a run under --reload, this time double-firing instead. Needs uvicorn/APScheduler log inspection to confirm, not more DB queries.

2. Build per-combo idempotency Structural fix for #1 (and any future cause of the same symptom): key on ticker+window+budget+day so a job firing twice can never again silently create duplicate positions. Directly explains today's RGTI showing 24 tracked_positions rows (six 4-budget sets) instead of the expected 5. Hard prerequisite for #5.

3. Shared retry-queue mechanism One utility, used by after_hours_batch's per-ticker loop AND paper_trade_open's per-combo pool: run the normal pass, collect exactly which items failed/timed out, wait a short buffer, retry once, log clearly whatever still fails (no infinite loop). Would have directly prevented today's "7 unfinished futures" from half-writing into a job that reported itself a total failure.

4. Wire iv_history into prediction The existing "predictive IV-expansion" signal: catch IV expanding ahead of a price move, distinct from OI buildup and from momentum (inherently reactive). Paired with a simple anti-chasing rule - don't recommend a ticker that's already moved >=5% today.

5. Expand scheduling to multiple times/day, capped at ~20 total Morning/mid-day/afternoon/near-close, simulating "if I checked right now, what would I find." CORRECTED: the ~20 target is TOTAL unique picks across the whole day, combined across every run - not ~20 per run (which would have meant up to ~100/day, mostly repeats of the same tickers). Each later run needs to know what earlier runs already opened today and either skip a ticker/combo already represented, or only add something genuinely new - the same underlying coordination problem as #2's idempotency, just at the "total daily volume" level instead of the "exact combo" level, so #2 should solve both together rather than as two separate mechanisms. Deliberately last - only safe once #2 and #3 are real, otherwise this multiplies today's exact bug on purpose. Open question, not yet decided: is ~20 split evenly across options/stock (~10 each), or ~20 total combined across both types?

6. Fix check_fills() Auto-detects fills by comparing live Webull positions against today's recommendations - matches on bare ticker only, not strike/expiry/type. Already caused one real mislabeled fill (an existing MSTR position auto-matched to an unrelated recommendation). DB rows cleaned up; root cause not yet fixed.

7. Returning-user login path login_with_invite() is the only way in and requires a still-pending invite_code every time. Once accepted, the only way back in is a manual DB reset. Blocks inviting real beta users, who have no database access.

8. MCP key regeneration Keys mint once at account creation with no way to reissue if lost. create_api_key() already supports multiple active keys per user - just needs a self-serve endpoint.

9. Cloud deployment Cloudflare tunnel or Railway.app. Directly unblocks #10 - multi-tenant MCP access is fully built and tested but has nowhere to actually run for a real customer yet.

10. Hosted MCP server MCP_TRANSPORT=http + ApiKeyTokenVerifier exist and are wired in, purely blocked on #9.

11. get_bars() wrapper bug Silently ignores the multiplier argument, only maps minute/hour/day/ week timespans - multiplier=5 would NOT actually fetch 5-minute bars through it. intraday_entry.py already works around this by calling unusual_whales.get_ohlc() directly; the wrapper itself is still a trap for any future caller.

12. Wire unused UW data into scoring Economic calendar and net-flow-by-expiry are never called - plausible real signal upgrades. Congress trades and insider-ownership-pct tools exist but don't feed conviction scoring or the LLM prompt at all.

13. ETF scoring in stock scans Currently a technicals-only stand-in since ETFs don't have analyst targets/PEG/revenue-growth. Works, but a real fund-appropriate model (expense ratio, index-tracking quality) would be better.

14. 3m "both" horizon's stock half Still uses horizon_engine.py's naive per-ticker loop, not smart_stock_scan.py's composite pre-filter (unlike pure 6m/1yr stock scans). Narrow, not urgent.

15. monitor_config table Schema exists with plausible columns (is_active, check_interval, total_alerts_fired) but usage by position_monitor.py not yet confirmed either way.

16. ChromaDB Running in docker-compose.yml, zero imports anywhere in the codebase. Either remove the container, or wire it into real RAG instead of context_builder.py's current direct-API-call approach.

17. conviction.py cleanup Orphaned - its only caller (the old daily_engine.py path) was retired this session. No remaining importers. Delete it or repurpose it.

18. tracked_positions.daily_rec_id Column exists in schema, not yet populated by the current fill- tracking flow (which matches by ticker + fill-price proximity instead). A more direct link; low priority, current approach already works and is tested.

19. Revisit paper-trading calibration assumptions All of these resolve the same way - once the reliability fixes above land and a real week or two of trading-hours data accumulates, not before:

Phase 4 grid sizing (windows/budgets) - untuned, watch job_run_log yield over time
STOCK_MIN_FUNDAMENTAL=60 - rejected every stock candidate on the one (after-hours) night tested; top priority to revisit ONLY if it recurs on a real trading day
The 25% spot-sanity threshold - "deliberately generous," never tuned against real data
iv_trend's "+5%" expansion threshold - provisional; zero tickers have accumulated enough iv_history yet to even populate this field
RSI/MACD/EMA9 entry-timing rule thresholds - standard convention, unvalidated against this system's real outcomes
5-min vs 15-min comparison - mechanism is live (Phase 6's timeframe_comparison), correctly reported "insufficient data" on week one's n=4 sample
MIN_SAMPLE_SIZE=5, window-length buckets, and the 15-min wrong- entry tiebreak - confirmed implemented exactly as specified; the open question is whether they're the RIGHT numbers, not whether they were built
Whether paper trades actually use the discussed 40%/50% options / 15%/25% stock stop/target defaults - never explicitly re-confirmed
The -355.6% loss from Phase 5's first real run - very likely a BSM-estimate-vs-live-price artifact (pre-market synthetic entry price vs a real close price), mechanism proven correct, magnitude needs re-checking on a real market-hours run

20. Real broker connections Robinhood/IBKR/Tastytrade - factory.py's abstraction and SnapTrade plumbing are ready; the actual OAuth/positions/orders code for each isn't written yet.

21. Versioned migrations Schema changes this session (fill-tracking columns, excluded_from_stats, users.is_admin, the strategy_recommendations rename, all the paper- trading tables) only exist as commands run directly against the live DB, not as files in db/migrations/.

22. requirements.txt typo black>=24.0% should be black>=24.0. Flagged previously, never verified fixed.

23. Open-source readiness sweep .env.example, public README setup video, a hardcoded-user-ID check across the codebase.

24. Architecture diagrams Two diagrams (backend + frontend), suitable for a public write-up. Deliberately deferred until the architecture stops moving quarter to quarter.

25. Interview talking points No dependencies, can happen anytime.

26. WebSocket real-time updates

27. Mobile push notifications PWA service worker.

28. Public invite page

Completed (see git log for full commit-level detail)
Multi-user MCP access - per-request identity resolution, HTTP transport, auto-minted customer keys, StockBros key-reveal screen
Duplicate prediction engines consolidated - MCP and web now share one engine (rescan_engine.py); found/fixed a real TypeError silently breaking every web options scan in the process
System B - horizon_engine.py takes real trading_window_days/stop/ target inputs instead of horizon buckets, both branches verified
BrokerNotConnectedError 500s fixed - backend + frontend, portfolio is admin-only end to end
New endpoints - open-positions (non-admin "portfolio" equivalent), admin all-users history view
Full frontend UI redesign - watchlist (Default/My two-section), history (admin toggle), picks tab (open positions), portfolio gating, 5-field recommendation form replacing horizon buckets
Flow/dark-pool scoring fix - was silently zeroed since day one, found in 5 files, centralized
OI buildup + market regime signals built, both genuine leading indicators
Trade math - real R/R gate, probability-adjusted EV gate, $50K sanity cap, unified strategy naming
Backtest/mark-to-market integrity - 8 corrupted historical rows excluded, credit-strategy P&L denominator bug fixed
Real target/stop tracked per fill (was hardcoded +20%/-40%); position monitor auto-resumes on restart
strategy_recommendations fully retired in favor of daily_recommendations
Watchlist unified - zero broker dependency, all hardcoded ticker lists removed, add-only sync
Stock scan quality - analyst-upside reliability discount, ETF scoring crash fixed
Progress bar's real root cause fixed (blocked event loop, not browser backgrounding)
Paper-trade-open job built - grid sweep across windows/budgets for both options and stock, full context snapshot per pick; 3 real identity/dedup bugs found and fixed along the way
IRON_CONDOR strike-order bug fixed (missing swap branch, produced structurally impossible negative max_loss); added a general structural-impossibility backstop for any future strategy shape
Paper-trade-close job built - real mark-to-market pricing, not a naive (exit-entry)/entry calc; found/fixed 2 real mark_to_market.py bugs (option-chain limit truncation, zero-bid legs wrongly treated as unpriceable) that blocked ANY option mark-to-market, not just paper trades
Fixed a separate, pre-existing production bug found along the way: log_outcome/log_exit naming mismatch was breaking every real trade exit (web and MCP) with an ImportError
Weekly strategy review (Phase 6) built - turns paper-trade outcomes into falsifiable per-bucket win-rate stats, verified against the first real week of data (correctly reported "insufficient sample" everywhere, as it should for n=4)
Rule-based intraday entry-timing signal (5-min/15-min) built, observational only; found/fixed a real bug where every intraday bar timestamp was computing to 0
After-hours batch job built - real daily history for TA/ fundamentals/insider activity/IV across the whole watchlist; found and fixed 4 stacked bugs in EDGAR insider-activity fetching that had made it 100% non-functional since it was built
Architecture Decisions Locked
- UW for OHLC bars; Polygon grouped_daily for scanner prices;
  yfinance for VIX; Ollama local for LLM
- Separate repos: trading-platform (backend) vs stockbros (frontend)
- FastAPI on :8001; MCP supports stdio (local admin) and HTTP (hosted,
  multi-customer) via MCP_TRANSPORT
- Invite code auth, no OAuth yet - but see #7, no path back in once
  accepted
- user_watchlist + is_admin is the entire watchlist system - no
  broker dependency for scanning, no hardcoded fallback lists anywhere
- daily_recommendations is the single source of truth for
  recommendations AND fill/outcome tracking
- watchlist_sync is add-only
- One scan engine per job, no fallback to a second implementation - a
  failure surfaces as a real error, never a silent degrade
- Portfolio (live Webull) is admin-only, backend and frontend; every
  other user's equivalent is their own confirmed-filled recommendations
- Paper-trading (source='auto_paper') is strictly additive to real
  trades - separate dedup rules, separate DB unique index, closed via
  its own function rather than log_exit()
- Every weekly-review statistic is a plain SQL/Python aggregation - an
  LLM call only ever phrases already-computed numbers, never produces one
Data Quality Notes
Weekend/holiday scanner behavior: flow/dark-pool signals go near-zero,
TA/IV rank still reliable, recommendation quality shifts to momentum+
TA only. Correct, unchanged behavior.

Paper-trading pipeline (all phases): fully built, each piece
individually verified, but every real run so far has happened
after-hours against estimated (not live) prices, with only one real
week of outcome data (n=4 trades). Treat outcome MAGNITUDES and every
"is X predictive" question as unconfirmed until the pipeline runs
start-to-finish across several real market-hours days - the mechanism
itself (signs, scaling, columns, isolation from real trades, honest
reporting of insufficient sample sizes) is already confirmed
independent of that.