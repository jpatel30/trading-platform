# Remaining Items

Last updated: July 2026

Last updated: July 2026. Full technical narrative (root causes, exact fixes, line-by-line verification) for completed work lives in git log commit messages - this doc stays a scannable status/priority list, not a technical diary.

Priority Order (Remaining)
Deploy somewhere real (Cloudflare tunnel / Railway)
Point the hosted MCP server at that deployment
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
smart_engine.py's LLM confidence output barely varies with input signal strength
Descriptions

1. Cloud deployment Cloudflare tunnel or Railway.app. Directly unblocks #2 - multi-tenant MCP access is fully built and tested but has nowhere to actually run for a real customer yet.

2. Hosted MCP server MCP_TRANSPORT=http + ApiKeyTokenVerifier exist and are wired in, purely blocked on #1.

3. monitor_config table Schema exists with plausible columns (is_active, check_interval, total_alerts_fired) but usage by position_monitor.py not yet confirmed either way.

4. ChromaDB Running in docker-compose.yml, zero imports anywhere in the codebase. Either remove the container, or wire it into real RAG instead of context_builder.py's current direct-API-call approach.

5. conviction.py cleanup Orphaned - its only caller (the old daily_engine.py path) was retired this session. No remaining importers. Delete it or repurpose it.

6. tracked_positions.daily_rec_id Column exists in schema, not yet populated by the current fill- tracking flow (which matches by ticker + fill-price proximity instead). A more direct link; low priority, current approach already works and is tested.

7. Revisit paper-trading calibration assumptions All of these resolve the same way - once the reliability fixes above land and a real week or two of trading-hours data accumulates, not before:

Phase 4 grid sizing (windows/budgets) - untuned, watch job_run_log yield over time
STOCK_MIN_FUNDAMENTAL=60 - rejected every stock candidate on the one (after-hours) night tested; top priority to revisit ONLY if it recurs on a real trading day
The 25% spot-sanity threshold - "deliberately generous," never tuned against real data
iv_trend's "+5%" expansion threshold - provisional; zero tickers have accumulated enough iv_history yet to even populate this field
RSI/MACD/EMA9 entry-timing rule thresholds - standard convention, unvalidated against this system's real outcomes
5-min vs 15-min comparison - mechanism is live (Phase 6's timeframe_comparison), correctly reported "insufficient data" on week one's n=4 sample
MIN_SAMPLE_SIZE=5, window-length buckets, and the 15-min wrong- entry tiebreak - confirmed implemented exactly as specified; the open question is whether they're the RIGHT numbers, not whether they were built
Whether paper trades actually use the discussed 40%/50% options / 15%/25% stock stop/target defaults - never explicitly re-confirmed
The -355.6% loss from Phase 5's first real run - very likely a BSM-estimate-vs-live-price artifact (pre-market synthetic entry price vs a real close price), mechanism proven correct, magnitude needs re-checking on a real market-hours run
job_run_log has no user_id column - fine now that paper-trade jobs run once against the admin account (not looped per user), but any OTHER job that still loops over active users (after_hours_batch, velocity_snapshot, nightly_learning, weekly_strategy_review) produces indistinguishable rows per user if it ever needs the same kind of forensic reconstruction paper-trading's scheduler investigation required

8. Real broker connections Robinhood/IBKR/Tastytrade - factory.py's abstraction and SnapTrade plumbing are ready; the actual OAuth/positions/orders code for each isn't written yet.

9. Versioned migrations Schema changes this session (fill-tracking columns, excluded_from_stats, users.is_admin, the strategy_recommendations rename, all the paper- trading tables) only exist as commands run directly against the live DB, not as files in db/migrations/.

10. requirements.txt typo black>=24.0% should be black>=24.0. Flagged previously, never verified fixed.

11. Open-source readiness sweep .env.example, public README setup video, a hardcoded-user-ID check across the codebase.

12. Architecture diagrams Two diagrams (backend + frontend), suitable for a public write-up. Deliberately deferred until the architecture stops moving quarter to quarter.

13. Interview talking points No dependencies, can happen anytime.

14. WebSocket real-time updates

15. Mobile push notifications PWA service worker.

16. Public invite page

17. smart_engine.py's LLM confidence output barely varies with input signal strength Found while verifying the new congress-trades/institutional-ownership/market-tide/econ-calendar wiring into _enrich_ticker/_compress_ticker/_build_llm_prompt (now live and confirmed present in real prompts - see git log). Ran the local Qwen model (temperature=0.05) on the same NVDA candidate with the new fields absent vs. present with extreme synthetic values (0buy/8sell vs 9buy/0sell congress, 5/100 vs 99/100 institutional ownership) - confidence stayed pinned at 72 in every run. Broadened the test to swap flow_score/dp_score/sweeps/RSI/trend/OI/IV-expansion between extreme-bullish and neutral/conflicted values (unrelated to this task's new fields) - confidence stayed at 72 there too, so this is a pre-existing characteristic of the model/prompt config, not something introduced by or specific to the new wiring. The reasoning text does visibly change to reference the new fields (e.g. "despite bullish market sentiment"), so the data isn't silently ignored - it just doesn't move the numeric confidence that becomes conviction_score. Worth a real fix (higher temperature, explicit confidence-calibration instructions, or a deterministic adjustment layered on top of the LLM number) but out of scope for the data-wiring task that surfaced it.

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
Scheduler "double-fire" diagnosed and fixed - NOT a scheduler misfire (restart, sleep/wake, coalesce/max_instances were all directly ruled out with real evidence); the paper-trade jobs were looping over every active user instead of firing once, so 2 active users produced 2 job_run_log rows that looked like a duplicate fire. Fixed: these jobs now run exactly once, always against the admin account. Also found and fixed a real, separate bug along the way: _intraday_context() (used by both open jobs) had no try/except, so any transient failure there silently aborted the entire run mid-loop with zero job_run_log trace, even though earlier windows' confirmed trades were already real DB rows - now resilient, with per-window defense-in-depth on top
Per-combo (ticker+window+budget+day) idempotency built as a structural safety net independent of the scheduler fix above - added trading_window_days/budget directly to tracked_positions (previously only on paper_trade_context), an app-level pre-check right before confirm_execution() that skips (and logs distinctly) an exact combo already opened today, and a DB-level partial unique index on (source, symbol, entry_date, trading_window_days, budget) WHERE source='auto_paper' as a real backstop. Verified live: ran the same day's admin scan twice back to back - the second run skipped every exact combo the first had opened (zero new rows, confirmed via direct count), a genuinely new ticker that won the same window on the second run still opened correctly (not over-blocked), and the same ticker across two different windows was confirmed independent (never compared against each other, by construction of the check). Also directly confirmed the DB constraint itself rejects a raw duplicate insert.
Credit-spread P&L sign bug fixed (mark_to_market.py) - today's first real paper-trade close reported a SPY iron condor at exit_price=-3.56, pnl_pct=+543.6%, contributing $95,245 of fake profit. Root cause (verified with real live quotes, not assumed): get_current_option_value()'s "current cost-to-enter this exact spread" return is legitimately negative for a healthy credit spread (same sign convention as entry_debit by design) - the credit branch's OLD formula (`abs(entry_debit) - current_value`) treated that negative number as if it were already a positive "cost to close," which effectively ADDED the entry credit and the current credit instead of taking their difference. Fixed by unifying both branches on the single correct formula, `pnl_per_share = current_value - entry_debit` (mathematically verified correct for both debit and credit trades - the debit branch already used this; only the credit branch was wrong). Also added a structural sanity bound (same category as strategy/engine.py's max_l_c<=0 check and smart_engine.py's spot-sanity check): pnl_dollars can never exceed the position's own max_profit or fall below -max_loss (10% buffer for STRADDLE/STRANGLE's max_profit being an estimate, not a hard ceiling) - treats a violation as "could not mark" rather than writing an impossible number. Verified live: the real SPY position now marks at exit_price=-3.70, pnl_pct=-2.3% (a tiny, sane, near-breakeven loss); confirmed TSLA/QQQ/RGTI's already-correct P&L (debit spreads and stock, unaffected by the credit-branch bug) remained untouched by the fix.
Shared retry-queue mechanism built (app/utils/retry_queue.py::run_with_retry) - one utility now used by all three call sites that previously each had their own ad-hoc partial-failure handling: after_hours_batch's per-ticker loop, paper_trade_open_options/_stocks's per-budget pool, and paper_trade_close's per-position loop. Runs the normal best-effort pass, waits 45s, retries only what failed once, logs whatever still fails as genuine and persistent (no infinite loop). Also fixes the related bookkeeping bug: a "partial" status no longer collapses real partial progress into tickers_processed=0 - job_run_log now separately tracks succeeded_first_pass/succeeded_on_retry/failed_both_passes. Verified live against today's real remaining state: re-ran the actual close job after resetting the 4 corrupted SPY positions and against 8 real still-open SMCI positions - the SMCI retry was genuine (real 45s wait, real second attempt, both logged with attempt="retry") and correctly still failed both passes (root cause: SMCI's stored expiry isn't a real listed expiry at all, not a transient issue - see the new priority-list item), while the 4 SPY positions and 4 other real SPCX positions closed correctly on the first pass with the bookkeeping accurately showing tickers_processed=8 alongside tickers_failed=12 rather than 0.
IV-expansion signal built (app/signals/iv_expansion.py::get_iv_expansion_signal) plus a quick_scan.py anti-chasing pre-filter - real recommendation-generation inputs, not post-hoc analysis. paper_trading.py's own _iv_context already computed a 5-day IV rate-of-change inline for paper_trade_context's iv_5day_trend field - extracted into this shared module (same "build shared logic once" principle as flow_scoring.py/trade_windows.py) rather than reimplementing; _iv_context is now a thin adapter over it. Score uses the same LEVEL-vs-VELOCITY + persistence-multiplier shape as oi_flow.py's get_oi_buildup_signal (up to 2x weight for consecutive same-direction days) - directionally NEUTRAL by design (rising IV means a bigger expected move, not which way), unlike OI buildup's bull/bear signal. Wired into quick_scan.py as Signal 6 (cached DB read, zero added API cost, contributes context not a bull/bear vote) with the anti-chasing rule (|change_pct|>=5% today = pre-filtered out, before scoring) applied ahead of it, and into smart_engine.py's _compress_ticker/_enrich_ticker so it's visible in the real LLM prompt. Verified live: called directly against the real 131-ticker watchlist - honestly reported 7/131 tickers with enough real history for a score (Phase 2's after-hours batch has only been running a few days) and 124/131 correctly INSUFFICIENT_HISTORY, no fabricated scores; ran real quick_scan() and confirmed 8 real tickers that moved >=5% today were completely absent from results while 5 real tickers under 5% were present; ran a live rescan_with_validation() (real scan/enrichment, LLM call mocked to capture the prompt) and confirmed IVexp: appears in every compressed candidate block of the actual 8,945-char prompt text, both for insufficient-history and real-score cases.
Rule-based intraday entry-timing signal (5-min/15-min) built, observational only; found/fixed a real bug where every intraday bar timestamp was computing to 0
After-hours batch job built - real daily history for TA/ fundamentals/insider activity/IV across the whole watchlist; found and fixed 4 stacked bugs in EDGAR insider-activity fetching that had made it 100% non-functional since it was built
Architecture Decisions Locked
- UW for OHLC bars; Polygon grouped_daily for scanner prices;
  yfinance for VIX; Ollama local for LLM
- Separate repos: trading-platform (backend) vs stockbros (frontend)
- FastAPI on :8001; MCP supports stdio (local admin) and HTTP (hosted,
  multi-customer) via MCP_TRANSPORT
- Invite code auth, no OAuth yet - but see #2, no path back in once
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