# Remaining Items

Last updated: July 2026

---

## Completed This Session (July 2026)

Grouped by theme - see git log for full commit-level detail.

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
- [ ] MCP tool cleanup, remaining pieces:
  - Expose market regime, OI buildup, and the signal-conflict flag as
    their own MCP tools (built and live in the web dashboard's scan
    pipeline; invisible to Claude Desktop right now)
  - Routing-note docstrings added to the 5 recommendation tools and
    scan_for_horizon/scan_watchlist (done, see MCP_TOOLS.md) - worth
    a final pass once the above tools are added
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

---

## Later

- [ ] Multi-user MCP access - blocked on get_current_user_id()'s
      single-cached-identity design (fine for one local user, not
      for multiple simultaneous users on a shared server instance)
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
- FastAPI wrapper on :8001 (MCP stays stdio, not HTTP)
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