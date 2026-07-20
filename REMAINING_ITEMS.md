# Remaining Items

Last updated: July 2026

---

## Completed This Session (July 2026)

Grouped by theme - see git log for full commit-level detail.

Multi-user MCP access (unblocks monetizing via Claude Desktop, not just
the web dashboard):
- get_current_user_id() no longer caches identity at process level -
  resolves fresh per call, from a per-request AccessToken under HTTP
  transport or the local MCP_API_KEY under stdio
- New ApiKeyTokenVerifier (app/mcp_server/auth.py) - verifies each MCP
  request's bearer token against user_api_keys independently, so one
  shared hosted server process can safely serve many simultaneous
  customers
- New MCP_TRANSPORT setting (stdio default, http for hosted) wired into
  server.py's entry point
- Customer MCP keys now minted automatically at account creation
  (/api/auth/login, new-user branch only) using the same
  generate_api_key()/create_api_key() the admin's key was created with,
  returned once in plaintext for StockBros to display
- Still open: no hosted server actually deployed/reachable yet, no
  self-serve key regeneration if a customer loses theirs

Duplicate prediction engines consolidated (MCP and web were silently
diverging on the same watchlist, same day - a real correctness risk
now that MCP customers are paying users, not just the admin):
- daily_engine.py::run_daily_recommendations() (the old, MCP-only scan
  path - conviction.py's 12-factor scoring, per-ticker LLM call, no
  user-driven stop/target, no market-regime awareness) retired. MCP's
  get_daily_recommendations now calls rescan_engine.py::rescan_with_
  validation() directly - the exact engine the web dashboard's Scan
  button uses. Ported over the two things the old path had that the
  new one didn't: portfolio-position exclusion from new BUY candidates,
  and a pre-flight API health check. Fixed a real $0 target_price/
  stop_price bug in rescan's own DB writes while merging.
- Found and fixed a second, independent bug in the process: the web
  API's /api/recommendations/daily endpoint was calling
  rescan_with_validation(..., horizon=horizon) - a parameter that
  doesn't exist on that function (it takes trading_window_days, not a
  horizon string, since a July rewrite). This raised TypeError on
  every single options-scan request, meaning the web dashboard's daily
  options scan had ALSO been silently falling back to the old
  daily_engine path on every call, not just occasionally on failure.
  Fixed by mapping horizon -> trading_window_days via horizon_engine's
  own HORIZON_CONFIG DTE ranges, with optional explicit
  trading_window_days/stop_loss_pct/profit_target_pct overrides for
  when the frontend is ready to send real user inputs. The "fallback to
  daily_engine on any exception" was then removed entirely per explicit
  instruction - a scan failure now surfaces as a real error instead of
  silently degrading.
- scan_for_horizon's stock-horizon scans (6m/1yr) now delegate to
  smart_stock_scan.py's composite fundamentals+velocity+insider
  pre-filter (previously web-only) instead of a naive, unfiltered
  per-ticker loop - MCP customers get the same pick quality as the web
  dashboard for watchlist-wide stock scans. (3m "both" horizon's stock
  half still uses the per-ticker loop - narrower case, not touched this
  pass.)
- The reliability discount on analyst-target upside (found and fixed
  for smart_stock_scan.py's ranking earlier this session) is now shared
  via fundamentals.py::analyst_target_reliability() and also applied
  inside horizon_engine.py::get_stock_for_horizon()'s target_price and
  score_fundamentals()'s fundamental-score gate - previously the
  discount affected only WHICH tickers got selected in a watchlist
  scan, not the actual dollar target_price/gate math shown to any
  single-ticker caller (get_stock_recommendation, get_horizon_
  recommendation) or even smart_stock_scan's own Phase 2 output.
- conviction.py (the old 12-factor scorer) is now orphaned - no
  remaining importers in app/. Left in place, not deleted; see below.

Predictive signals & scoring correctness:
- Flow/dark-pool scoring was silently zeroed since day one (wrong
  field checked) - found independently in 5 files, centralized in
  signals/flow_scoring.py
- OI buildup signal built (signals/oi_flow.py) - genuine leading
  indicator, not reactive to a move already in progress
- Market regime built (signals/market_regime.py) - VIX term
  structure + put/call ratio
- Scanner signal bugs fixed: tie resolution, conflict detection,
  flow threshold, dark-pool independent voting

Trade math:
- Real risk/reward gate (actual strike economics, not a constant ratio)
- Probability-adjusted EV gate for debit/long-premium strategies
- $50K/contract sanity cap; iron condor width now type-based, not
  position-based; option-symbol parsing fixed for tickers containing
  C/P (CRM, COIN, PYPL, etc.)
- Unified strategy naming (STRATEGY_ALIASES / normalize_strategy())

Backtest & mark-to-market integrity:
- 8 historical rows excluded from stats (phantom $1M+ profits,
  structural max_profit>max_loss impossibilities)
- Mark-to-market P&L% denominator bug fixed for credit strategies
  (was dividing by credit received instead of real capital at risk)
- excluded_from_stats filter added retroactively to 4 queries that
  were missing it after the strategy_recommendations migration

Fill tracking & alerts:
- Real target/stop now tracked per confirmed fill (was hardcoded
  +20%/-40% regardless of the actual recommendation)
- Position monitor auto-resumes on server restart (previously
  required manual restart every time)
- Confirmed both take-profit AND stop-loss Discord alerts already
  existed and work correctly (position_monitor.py + sell_signals.py)

strategy_recommendations to daily_recommendations migration:
- Added fill-tracking columns to daily_recommendations
  (user_executed, actual_entry_price, exit_price, actual_pnl, etc.)
- Rewired confirm_execution/log_exit, nightly_loop.py,
  backtester.py (both backtests), learning/engine.py - all now
  read/write daily_recommendations directly
- Old table renamed to strategy_recommendations_deprecated, fully
  unreferenced, verified via repo-wide grep

Watchlist unification (no broker dependency):
- get_scan_universe() rewritten - reads entirely from
  user_watchlist, zero broker calls
- users.is_admin flag - admin's own watchlist rows ARE the shared
  default for every user, no separate table needed
- All hardcoded ticker lists removed: EXCLUDED set, SP500_SUPPLEMENT,
  MARKET_PROXIES, the [NVDA, AAPL] stock fallback, and
  DEFAULT_UNIVERSE itself
- horizon_engine.py's separate, parallel watchlist implementation
  folded into the same scanner.universe.get_scan_universe()
- 126-to-131 ticker gap closed (5 tickers missing from DB, backfilled)
- watchlist_sync.py destructive-delete bug fixed - sync is now
  add-only, previously could silently delete a manually-added ticker
  not mirrored in the broker

Stock scan quality:
- Reliability discount added to analyst-target upside (thin coverage
  / wide analyst disagreement / low share price all reduce trust) -
  this was the actual mechanism behind picks clustering under $30
- ETF crash fixed (dict.get() default-value gotcha in
  fundamentals.py - .get(key, default) only applies default when
  the key is absent, not when it's None) - ETFs no longer excluded
  from stock scans and no longer crash when included

Infrastructure:
- Progress bar's real root cause fixed: long-running scan endpoints
  blocked the single asyncio event loop, so status-polling requests
  queued as genuinely unprocessed for the full scan duration - not
  just a browser-backgrounding artifact as first suspected
- mark_all_active_recommendations() now runs before
  run_nightly_loop() in the same scheduled job - closes the loop
  from "recommendations get made" to "recommendations get evaluated
  and learned from, automatically, every day"

---

## Immediate / In Progress

- [ ] Frontend watchlist-mode checkbox - "Default Watchlist" /
      "Default + My Watchlist" on the Picks form. Backend
      (watchlist_mode param) is fully wired; just needs the UI control.
- [ ] Watchlist page investigation - page showed empty despite
      user_watchlist genuinely having 131 rows. Confirmed NOT a
      backend bug (get_db_watchlist() tested directly, returns
      correctly). Likely a stale frontend load or separate rendering
      issue - needs a fresh look in the browser, not more backend digging.
- [ ] tracked_positions.daily_rec_id - column exists in schema, not
      yet populated by the current fill-tracking flow (which matches
      by ticker + fill-price proximity instead). A more direct link;
      low priority, current approach works and is tested.

---

## Next Up

- [ ] Predictive IV-expansion signal - iv_history table exists
      (per-ticker IV snapshots over time) but nothing reads from it
      yet. Goal: catch IV expanding ahead of a price move (options
      markets often price in a move before it happens), distinct from
      OI buildup and from momentum (which is inherently reactive).
      Paired with a simple anti-chasing rule: don't recommend a
      ticker that's already moved >=5% today in either direction.
- [ ] monitor_config table - schema exists with plausible columns
      (is_active, check_interval, total_alerts_fired) but usage by
      position_monitor.py not yet confirmed either way.
- [ ] ChromaDB - running in docker-compose.yml, zero imports anywhere
      in the codebase. Either remove the container, or wire it into
      real RAG (news embeddings, historical similar-setups) instead
      of context_builder.py's current direct-API-call approach.
- [ ] ETF scoring in stock scans - currently a technicals-only
      stand-in (price vs 50d/52w-high, YoY change) since ETFs don't
      have analyst targets/PEG/revenue-growth. Works, but a real
      fund-appropriate model (expense ratio, index-tracking quality)
      would be better.
- [ ] UW underutilization - economic calendar and net-flow-by-expiry
      are never called; both are plausible real signal upgrades.
      Congressional trades and insider-ownership-pct tools exist but
      don't feed conviction scoring or the LLM prompt.
- [ ] Real closed trades - conviction-weight recalibration, all 3
      backtests, and strategy-performance analysis all need actual
      closed trades to activate (every test this session correctly
      reports 0). Structural - needs trading activity, not code.
- [ ] conviction.py cleanup - orphaned this session (its only caller,
      the old daily_engine.py scan path, was retired); no remaining
      importers in app/. Either delete it or repurpose it - currently
      just sitting there unused.
- [ ] 3m "both" horizon's stock half still uses horizon_engine.py's
      naive per-ticker loop, not smart_stock_scan.py's composite
      pre-filter (unlike pure 6m/1yr stock scans, fixed this session) -
      merging an independently-ranked composite scan into the existing
      per-ticker options+stock combined result shape needs more design
      than the pure-stock case did. Narrow (3m only), not urgent.
- [ ] MCP key regeneration - customer keys are minted once at account
      creation with no way to get a new one if lost; needs a self-serve
      endpoint (create_api_key() already supports multiple active keys
      per user).
- [ ] Hosted MCP server - MCP_TRANSPORT=http + ApiKeyTokenVerifier exist
      and are wired in, but nothing is actually deployed/reachable yet;
      blocked on the same cloud deployment item below.

---

## Later

- [ ] Real Robinhood/IBKR/Tastytrade implementations - factory.py's
      abstraction and SnapTrade plumbing are ready; the actual
      OAuth/positions/orders code for each isn't written yet
- [ ] Versioned migrations for schema changes applied ad-hoc this
      session (daily_recommendations fill-tracking columns,
      excluded_from_stats, users.is_admin, the strategy_recommendations
      rename) - currently only exist as commands run directly against
      the live DB, not as files in db/migrations/
- [ ] Requirements.txt typo (black>=24.0% -> black>=24.0) - flagged
      previously, not verified fixed
- [ ] .env.example, public README setup video, hardcoded-user-ID
      sweep for open-source readiness

## Future (When Monetizing / Publishing)

- [ ] Two architecture diagrams (backend + frontend), suitable for a
      public write-up - deliberately deferred until the architecture
      stops moving quarter to quarter
- [ ] Interview talking points - no dependencies, can happen anytime
- [ ] Cloud deployment (Cloudflare tunnel or Railway.app)
- [ ] WebSocket real-time dashboard updates
- [ ] Mobile push notifications (PWA service worker)
- [ ] Public invite page

---

## Architecture Decisions Locked

```
- UW for OHLC bars (not Polygon - 87x faster)
- Polygon grouped_daily for scanner prices (1 call, no UW equivalent)
- yfinance for VIX (UW VIX = 403 on monthly plan)
- Ollama local for LLM
- Separate repos: trading-platform (backend) vs stockbros (frontend)
- FastAPI wrapper on :8001; MCP now supports both stdio (local admin)
  and HTTP (hosted, multi-customer via ApiKeyTokenVerifier) via the
  MCP_TRANSPORT setting - no longer stdio-only
- Invite code auth (existing DB system, no OAuth needed yet)
- NEW: user_watchlist + is_admin flag is the entire watchlist system -
  no separate default-watchlist table, no broker dependency for scanning
- NEW: no hardcoded fallback ticker lists anywhere - an empty,
  honest result beats a silent, confusing substitute
- NEW: strategy_recommendations retired - daily_recommendations is
  the single source of truth for both recommendations AND fill/outcome
  tracking
- NEW: watchlist_sync is add-only - never deletes a manually-added
  ticker just because a connected broker doesn't have it
- NEW: one scan engine per job, no fallback to a second implementation -
  rescan_engine.py/smart_engine.py for daily options recs (MCP and web
  both), smart_stock_scan.py for watchlist-wide stock scans (MCP and
  web both) - a failure surfaces as a real error, not a silent
  degrade to a different, possibly-disagreeing engine
```

---

## Data Quality Notes

```
Weekend/holiday scanner behavior (unchanged, still correct):
  Flow signals: batch returns near-zero for most tickers (market closed)
  TA signals: based on last close - reliable
  IV rank: real from UW (not affected by market hours)
  Recommendation quality: shifts to momentum + TA only on these days

On market days: 50+ flow alerts with real sweeps, 100+ dark pool
prints with direction, OI buildup data from the previous session's
snapshot, market regime computed fresh.
```