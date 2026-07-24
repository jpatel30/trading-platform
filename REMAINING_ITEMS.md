# Remaining Items

Last updated: July 2026

---

## Completed This Session (July 2026)

Grouped by theme - see git log for full commit-level detail. Items
below marked (Claude Code) were done directly against the backend
repo by a separate Claude Code session working in parallel; everything
else was done in this conversation.

### Multi-user MCP access (Claude Code)
(unblocks monetizing via Claude Desktop, not just the web dashboard)
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
  (/api/auth/login, new-user branch only), returned once in plaintext
- StockBros signup screen built to display this key exactly once, with
  copy-to-clipboard and explicit "save this now" messaging (frontend,
  this session)

### Duplicate prediction engines consolidated (Claude Code)
(MCP and web were silently diverging on the same watchlist, same day)
- daily_engine.py's old MCP-only scan path retired; MCP's
  get_daily_recommendations now calls rescan_engine.py directly - the
  same engine the web dashboard uses
- Found and fixed a real TypeError bug in the process: the web API was
  calling rescan_with_validation(..., horizon=horizon) after that
  function's signature had already moved to trading_window_days - this
  had been silently falling every single web options-scan back to the
  old daily_engine path, not just occasionally on failure. Fixed with a
  horizon -> trading_window_days shim; the "fallback to daily_engine on
  any exception" was removed entirely - a scan failure now surfaces as
  a real error
- scan_for_horizon's 6m/1yr stock scans now use smart_stock_scan.py's
  composite pre-filter instead of a naive per-ticker loop
- Analyst-target reliability discount unified into fundamentals.py,
  shared across every caller (previously only affected watchlist-scan
  ranking, not single-ticker target_price/gate math)
- conviction.py (old 12-factor scorer) orphaned - no remaining
  importers; left in place, not deleted

### System B - horizon_engine.py real user inputs (Claude Code)
- get_options_for_horizon, get_stock_for_horizon, scan_for_horizon all
  rewritten to take trading_window_days/stop_loss_pct/profit_target_pct
  directly, same backward-compatible shim pattern as the options branch
  (horizon stays a fallback for anything not yet sending the new
  params). Expiry selection now picks the real listed expiry closest to
  a computed target date, not a DTE-range midpoint.
- main.py's STOCK branch of /api/recommendations/daily wired to match -
  derives a window from HORIZON_CONFIG only where a real DTE range
  exists (3m); pure stock horizons (6m/1yr) pass None through to
  get_stock_for_horizon's own better fallback instead of a guessed
  synthetic day count
- A separate, independent double-fallback crash caught and fixed in the
  same pass (two consecutive strike-selection failures used to throw
  instead of degrading)
- Verified directly (this conversation): both branches confirmed
  correctly threading all 3 params through, not just documented as such

### BrokerNotConnectedError 500s fixed (Claude Code + this session)
- Backend: centralized handling instead of patching each endpoint
  ad-hoc - broker-dependent calls now degrade cleanly for any user
  without a Webull connection (every non-admin, by this session's
  design) instead of raising an uncaught 500
- Frontend: portfolio is admin-only by design, so rather than only
  handle the error gracefully, the dashboard now doesn't even attempt
  these calls for a non-admin - is_admin fetched once in layout.tsx,
  shared via React Context (useIsAdmin), consumed by page.tsx and
  portfolio/page.tsx. All three real call sites confirmed gated.

### New endpoints - open positions + admin history view (Claude Code)
- GET /api/recommendations/open-positions - a user's own confirmed-
  filled recommendations with live mark-to-market, real fill details,
  and their real target/stop thresholds. This is the non-admin
  equivalent of "portfolio" - no broker connection needed, applies to
  the admin too (a confirmed fill is separate from the real Webull
  account)
- GET /api/recommendations/history-grouped?all_users=true - admin-only
  (403 otherwise via new get_current_admin_user_id dependency), same
  underlying query minus the per-user filter, plus a by_user leaderboard
  summary (net P&L, win rate, picks per customer)

### Full frontend UI redesign (this session)
- Watchlist page: two-section display (Default Watchlist / My
  Watchlist) instead of a single flat list or a checkbox toggle -
  matches the backend's admin-watchlist-is-shared-default design
  directly. Both sections independently collapsible. For the admin
  specifically, "My Watchlist" shows an explanatory message instead of
  a duplicate of Default (structurally guaranteed identical for that
  one account, not a bug)
- History page: admin-only Mine/All Users toggle, by-customer
  leaderboard in All Users mode, each pick tagged with whose it is when
  viewing everyone's
- Picks tab: "My Open Positions" section reading the new open-positions
  endpoint, shown to every user regardless of admin status
- Portfolio: gated behind is_admin at all three real call sites
  (layout.tsx's persistent strip + 5-min poll, page.tsx, and the
  dedicated portfolio/page.tsx route which was never actually touched
  until specifically re-checked)
- Recommendation screen: full replacement of the horizon-bucket buttons
  (1W/2W/1M/3M/6M/1Y) with 5 direct user inputs - trade type, trade
  amount (rounds to nearest cent), trading window in days (1-365
  options / 1-730 stock), stop loss %, profit target % - all with
  inline validation hints, submit disabled until every field is valid

### Predictive signals & scoring correctness
- Flow/dark-pool scoring was silently zeroed since day one (wrong
  field checked) - found independently in 5 files, centralized in
  signals/flow_scoring.py
- OI buildup signal built (signals/oi_flow.py) - genuine leading
  indicator, not reactive to a move already in progress
- Market regime built (signals/market_regime.py) - VIX term
  structure + put/call ratio, exposed as its own MCP tool
- Scanner signal bugs fixed: tie resolution, conflict detection,
  flow threshold, dark-pool independent voting

### Trade math
- Real risk/reward gate (actual strike economics, not a constant ratio)
- Probability-adjusted EV gate for debit/long-premium strategies
- $50K/contract sanity cap; iron condor width now type-based, not
  position-based; option-symbol parsing fixed for tickers containing
  C/P (CRM, COIN, PYPL, etc.)
- Unified strategy naming (STRATEGY_ALIASES / normalize_strategy())

### Backtest & mark-to-market integrity
- 8 historical rows excluded from stats (phantom $1M+ profits,
  structural max_profit>max_loss impossibilities)
- Mark-to-market P&L% denominator bug fixed for credit strategies
  (was dividing by credit received instead of real capital at risk)
- excluded_from_stats filter added retroactively to 4 queries that
  were missing it after the strategy_recommendations migration

### Fill tracking & alerts
- Real target/stop now tracked per confirmed fill (was hardcoded
  +20%/-40% regardless of the actual recommendation)
- Position monitor auto-resumes on server restart (previously
  required manual restart every time)
- Confirmed both take-profit AND stop-loss Discord alerts already
  existed and work correctly (position_monitor.py + sell_signals.py)

### strategy_recommendations to daily_recommendations migration
- Added fill-tracking columns to daily_recommendations
- Rewired confirm_execution/log_exit, nightly_loop.py, backtester.py
  (both backtests), learning/engine.py - all read/write
  daily_recommendations directly now
- Old table renamed to strategy_recommendations_deprecated, fully
  unreferenced, verified via repo-wide grep

### Watchlist unification (no broker dependency)
- get_scan_universe() rewritten - reads entirely from user_watchlist,
  zero broker calls
- users.is_admin flag - admin's own watchlist rows ARE the shared
  default for every user, no separate table needed
- All hardcoded ticker lists removed: EXCLUDED set, SP500_SUPPLEMENT,
  MARKET_PROXIES, the [NVDA, AAPL] stock fallback, DEFAULT_UNIVERSE
- horizon_engine.py's separate, parallel watchlist implementation
  folded into scanner.universe.get_scan_universe()
- 126-to-131 ticker gap closed (5 tickers missing from DB, backfilled)
- watchlist_sync.py destructive-delete bug fixed - sync is now
  add-only, previously could silently delete a manually-added ticker
  not mirrored in the broker

### Stock scan quality
- Reliability discount added to analyst-target upside (thin coverage
  / wide analyst disagreement / low share price all reduce trust) -
  this was the actual mechanism behind picks clustering under $30
- ETF crash fixed (dict.get() default-value gotcha in fundamentals.py)
  - ETFs no longer excluded from stock scans and no longer crash when
  included

### Infrastructure
- Progress bar's real root cause fixed: long-running scan endpoints
  blocked the single asyncio event loop - not just a browser-
  backgrounding artifact as first suspected
- mark_all_active_recommendations() now runs before run_nightly_loop()
  in the same scheduled job

### Automated paper-trade-open job (grid sweep, observational)
Runs a grid of windows x budgets for both options and stock, auto-
confirming the highest-conviction pick PER COMBO (not one overall
winner/day) with a full context snapshot, so the weekly review has real
outcome data across many windows/amounts to find out which actually
correlates with wins. Depends on Phase 2 (ticker_daily_snapshot) and
Phase 3 (get_intraday_signal) built earlier this session; reuses
rescan_with_validation()/run_smart_stock_scan() exactly.
- New paper_trade_context table (full signal snapshot per pick: flow/
  dp/oi scores, IV level + 5-day trend, daily TA, both intraday
  timeframes' full signal, VIX/regime, conviction, and
  which_strategy_rule_fired) - created ad-hoc, same as this session's
  other schema changes.
- New app/recommendations/paper_trading.py -
  run_paper_trade_open_options/run_paper_trade_open_stocks(user_id).
  Budget doesn't change which ticker/strategy gets picked, only
  position sizing - so the expensive part (scan+enrich+one LLM call for
  options; scan+composite-ranking for stock) runs ONCE per window, not
  once per combo. The other 3 budgets per window only re-run the cheap,
  deterministic sizing step.
- Added a strategy_rule field to rescan_engine.py's LLM prompt/schema
  (the model now names which STRATEGY SELECTION rule it followed, e.g.
  "INDEX_NEUTRAL_IRON_CONDOR") plus flow/dp/oi/iv attached onto each
  final pick - both needed by paper_trade_context, neither existed on
  the returned pick before.
- Scheduled at 6:40am PT alongside the existing jobs.

Found and fixed three real bugs while verifying this live, none of them
about "does the LLM call work" - all about identity/uniqueness once the
same ticker gets traded multiple times a day, which only surfaces under
a real multi-combo grid:
1. confirm_execution()'s app-level "already tracked" guard was scoped
   to (symbol, day) - too coarse for a grid that deliberately wants
   multiple simultaneous positions on the same ticker/day. Extended
   with a source param ('recommendation' default, unchanged behavior;
   'auto_paper' skips the dedup guard entirely - the job's own loop is
   the sole caller and controls not double-calling for the same combo).
2. A separate, harder DATABASE-level unique index
   (idx_tracked_unique, on user_id+symbol+entry_date+entry_price) was
   still blocking multiple auto_paper rows even after the fix above -
   options at different budgets share the same per-contract entry
   price (only qty differs), so every budget after the first hit a
   real UniqueViolation. Replaced with a partial unique index scoped to
   source='recommendation' only.
3. paper_trading.py itself never checked confirm_execution()'s
   `confirmed` flag before counting a combo as confirmed - a silent
   DB-constraint failure was being counted as a success with a null
   tracked_position_id. Also found: top_pick for a window's reference
   budget can be a same-day "morning pick" reload (rescan_with_validation's
   own caching) rather than a freshly-executed trade, which has no
   "contracts" field - fixed by always recomputing trade math fresh via
   the resize helper, for every budget including the first.

FIXED: tonight's live IRON_CONDOR picks across all 5 windows had a
structurally impossible negative max_loss (wing width narrower than the
credit received) - strikes were also far from SPY's real spot price.
Root cause: `smart_engine.py::_execute_smart_rec` has strike-order
auto-correction (`buy_str`/`sell_str` swap) for DEBIT_CALL_SPREAD,
DEBIT_PUT_SPREAD, CREDIT_CALL_SPREAD, and CREDIT_PUT_SPREAD, but was
missing the equivalent branch for IRON_CONDOR - when the LLM/pipeline
handed back `buy_strike > sell_strike` for an iron condor, the wing
width went negative, producing a credit that exceeded the wing width.
Fixed by adding the missing
`elif strategy == "IRON_CONDOR" and buy_str > sell_str: swap` branch to
both the pre-snap and post-snap correction blocks in
`smart_engine.py::_execute_smart_rec`, mirroring the pattern already
used for the other 2-leg strategies. Also added a general defense-in-depth
backstop in `strategy/engine.py::_execute_trade_math`:
`if max_l_c <= 0 or max_p_c <= 0: raise ValueError(...)` right after the
existing per-contract sanity cap, so any future strategy/shape that
produces a structurally impossible max_profit/max_loss is rejected
instead of silently sailing through the R/R gate (which only ever
rejected LOW risk/reward). Both verified live independently: reproducing
the exact swap (buy_strike=750/sell_strike=725 SPY iron condor) now
auto-corrects and returns a sane positive max_loss trade; feeding
deliberately-inverted legs directly to `_execute_trade_math` (bypassing
the auto-correction) correctly raises the new structural-impossibility
error.

FIRST PRIORITY to investigate/fix if observed during real trading hours
(not fixed now, per explicit instruction - do not preemptively change
the gate value): STOCK_MIN_FUNDAMENTAL (=60, set earlier this session)
rejected every one of tonight's top-10 composite-ranked stock candidates.
Tonight this looked like a real but ordinary zero-pick night (after-hours,
low diversity), not a confirmed bug. But if this same zero-pick behavior
recurs on a live trading day (market open, this job's normal run window),
treat it as the top-priority item to dig into - check whether
STOCK_MIN_FUNDAMENTAL=60 is simply too strict for the current
composite-ranking distribution, before assuming it's expected.

### Automated paper-trade-close job (the other half of Phase 4)
Without this, Phase 4 only ever opened positions that never resolved -
no win/loss ever got produced for Phase 6 to eventually learn from.
- New app/recommendations/paper_trading.py::run_paper_trade_close(user_id) -
  scheduled 12:55pm PT (5 min before close), closes every auto_paper
  position with entry_date=CURRENT_DATE and is_active=TRUE. Writes the
  SAME daily_recommendations columns a real fill's exit uses (exit_price,
  actual_pnl, actual_pnl_pct, was_correct, closed_at) so Phase 6 can query
  paper and real trades identically.
- Deliberately does NOT call log_exit()/log_outcome() - that function
  picks "the most recently-entered active position for this symbol",
  which is ambiguous the moment more than one window/budget combo on the
  same ticker is still open (exactly what this grid does), and its P&L
  is a plain (exit-entry)/entry, which mis-prices any credit strategy the
  same way mark_to_market's pnl_pct was wrong before that was fixed
  (entry_debit is stored negative for credit trades). Instead, each
  tracked_positions row is closed by its own id, matched to its exact
  daily_recommendations row via paper_trade_context (written by the open
  job), and priced with mark_to_market.py's own debit/credit-aware
  mark_recommendation() - reusing both its fetch functions AND its P&L
  math, not just the fetch.
- **Found and fixed two real bugs in mark_to_market.py while verifying
  this against today's real auto_paper positions (root cause, not a
  workaround - both bugs block ANY option mark-to-market, not just
  paper trades):**
  1. get_current_option_value() hardcoded limit=200 when fetching the
     option chain for a single expiry. SPY's real chain for one expiry
     was 334 contracts - the truncation silently dropped the deep
     ITM/OTM wing strikes a 3-day iron condor actually used, so 3 of 4
     legs never matched and the whole spread came back unmarkable.
     Fixed: limit=500.
  2. Even after the limit fix, deep OTM legs with bid=0/ask=0.01 (real
     and near-worthless, not "no data") were still being skipped - the
     old `mid = (bid+ask)/2 if (bid and ask) else ...` treated bid=0 as
     disqualifying, and mid=0 was then treated as "no data" too. Since
     ALL legs must match for a spread to be valued, one near-worthless
     wing blocked the entire position - exactly the common shape for
     iron condor wings once a position is winning. Fixed: compute mid
     from whichever side(s) are actually quoted, only skip when bid,
     ask, AND mid/last_price are all literally zero.
- Verified live against today's real auto_paper positions (4 SPY IRON_CONDOR
  combos from the 3-day window, budgets $1k/$2.5k/$5k/$10k - the only
  window that produced a valid pick tonight, see below): all 4 closed
  cleanly (0 errors), pnl_dollars/pnl_pct were real, non-null, non-zero,
  and scaled linearly with qty as expected (-$2,084/-$4,168/-$10,420/
  -$20,840 for qty 1/2/5/10, all -355.6% pnl_pct). was_correct correctly
  matched the loss direction (all 4 False). Confirmed the daily_recommendations
  and tracked_positions rows both got the real values, on the right
  columns. Confirmed a prior day's 20 auto_paper rows (entry_date=
  yesterday) and all 8 real 'recommendation'-source rows were completely
  untouched by this run (query-scoped correctly).
- **Honest caveat on tonight's actual numbers:** the -355.6% loss on all
  4 is very likely a pre-market pricing artifact, not a real trading
  result - the entry legs were stored with "price_source": "BSM_estimate"
  (synthetic pricing used when the market is closed/thin), while the
  close job fetched real live bid/ask quotes minutes later. Comparing a
  synthetic entry estimate to a real live price will not produce a
  meaningful P&L number. The MECHANISM is proven correct (real distinct
  values, correct sign propagation, correct qty scaling, correct columns);
  the $ magnitude itself should be re-verified on a day this runs fully
  inside real market hours (both open at 6:40am PT and close at 12:55pm
  PT against live, non-estimated quotes) before trusting the actual pnl
  numbers it produces.
- **FIXED - a deeper root cause than the IRON_CONDOR strike-order bug:**
  during tonight's fresh open-job run (post strike-order-fix), window=60
  (SPY) and window=30 (QQQ) both hit the defense-in-depth
  structural-impossibility check and were correctly rejected rather than
  silently stored - e.g. `max_profit_per_contract=3836.0
  max_loss_per_contract=-3086.0` for an IRON_CONDOR whose strikes were
  already in the correct BUY/SELL order (so NOT the swap bug - a credit
  of $3,836 bigger than a $750 wing width means the strikes themselves
  were wrong, not their order). Root cause: the LLM prompt in
  rescan_engine.py already says "USE EXACT PRICES SHOWN ABOVE - do NOT
  use your training data prices" and "Max 8% OTM from current price," but
  nothing downstream ever actually enforced either rule in code - the
  local LLM (Qwen 14B) returned strikes ~40% away from SPY's real $738
  spot (439/459), almost certainly falling back on stale training-data
  price memory for a ticker/window it had less to anchor on. Fixed: added
  a spot-sanity check in smart_engine.py::_execute_smart_rec, right after
  the existing spot/buy_strike presence check and before any strike
  correction/snapping - rejects (returns None, same contract as this
  function's other invalid-input cases) any strike more than 25% from the
  real fetched spot price, with a clear log line naming the ticker,
  strategy, strike, and % distance. 25% is deliberately generous - it
  catches a 40%-off hallucination while still allowing a legitimately
  wide, longer-dated condor or strangle. NAKED_CALL/NAKED_PUT only checks
  buy_strike (sell_strike is intentionally 0 for those per the prompt).
  Verified live: feeding the exact reproduction (SPY spot=$738.18,
  buy_strike=439, sell_strike=459) now gets rejected immediately with
  `[SmartMath] Rejected SPY IRON_CONDOR: strike $439.0 is 41% from real
  spot $738.18`; feeding a legitimate near-spot condor (725/750, ~2-3%
  from spot) passes the sanity check cleanly and proceeds to the normal
  downstream strike-snapping/trade-math path (confirmed no false-positive
  rejection).
- **FIXED - separate, pre-existing, unrelated bug found while reading
  this code path (real and was currently live-breaking):** both
  app/api/main.py's `/api/execution/outcome` endpoint (~line 1459) and
  the MCP tool log_outcome() in app/mcp_server/server.py (~line 1148)
  did `from app.learning.prediction_tracker import log_outcome` - that
  function has never existed in prediction_tracker.py; the real function
  is named log_exit(). Because the import was inside the function body
  (lazy import), this didn't fail at server startup - it failed with a
  real ImportError the moment any actual user (web or MCP) tried to log a
  real trade exit, meaning closing out a REAL fill was broken in
  production for every caller. Fixed: both call sites now import and call
  log_exit() (the real, already-documented, already-used-elsewhere-in-
  comments internal name) instead of the nonexistent log_outcome(). The
  external MCP tool name (the decorator `log_outcome`, what a Claude
  Desktop user actually invokes) is unchanged - only the broken internal
  import was wrong.

### Rule-based intraday entry-timing signal (5-min/15-min, observational)
Sits between the overnight daily thesis and the actual paper trade open
(Phase 4) - doesn't gate anything in this build, just logs what the
intraday technical picture looked like at entry so Phase 6's weekly
review can later find out empirically which timeframe (if either)
correlates with wins.
- New app/signals/intraday_entry.py::get_intraday_signal(ticker,
  direction, timeframe) - RSI(14), MACD(12,26,9) histogram, price vs
  EMA9. BULLISH/BEARISH check specific rules (RSI recovering from
  oversold or rising in a 40-60 band, MACD histogram positive/turning
  positive, price above EMA9 - mirrored for BEARISH) and report which
  fired; NEUTRAL (credit/iron-condor) skips the directional check
  entirely and returns only the raw values.
- Found and fixed a real, blocking bug while building this:
  unusual_whales.py's get_ohlc() computed every intraday (5m/15m/1h/1m)
  bar's timestamp as 0 - it only ever read a "date" field, which daily/
  weekly candles carry but intraday candles don't (they carry
  start_time/end_time instead). Every bar sharing the same ts=0 made
  the function's own sort a no-op, silently leaving intraday bars in
  the raw API's actual order - confirmed descending/newest-first, the
  opposite of what RSI/MACD assume. Daily-candle behavior (which does
  carry "date") is unchanged; fixed by falling back to parsing
  start_time when date is absent.
- Verified live during real (after-hours, market closed for the day)
  conditions across 7 ticker/direction/timeframe combinations - RSI/
  MACD/EMA9 all genuinely varied (RSI 29.0-63.2 across tickers, not
  stuck on a neutral default), NEUTRAL direction confirmed to skip
  rule-checking while still returning real raw values.
- Empirically answered the warmup question (matters for how far back
  Phase 4 needs to pull data): 5-min bars stabilize by 2 trading days
  (156 bars) - RSI/MACD match the 10-day value exactly from there
  onward. 15-min bars need more: 1 day (26 bars) isn't even enough for
  MACD to be defined, meaningful convergence by ~5 days, fully
  converged by 7 days. Makes sense - 15-min bars carry ~3x less data
  per calendar day than 5-min, so the same 26-period MACD slow EMA
  needs proportionally more calendar days to warm up.
- Not fixed, flagged: market_data/uw_market_data.py's get_bars()
  wrapper doesn't support this - its timespan_map only maps "minute"/
  "hour"/"day"/"week" and silently ignores the multiplier argument
  entirely, so multiplier=5 would NOT actually fetch 5-minute bars
  through it. intraday_entry.py calls unusual_whales.get_ohlc()
  directly instead. Worth fixing the wrapper itself at some point so
  other future callers don't hit the same silent-wrong-granularity trap.

### After-hours batch: real history for TA/fundamentals/insider
"Predictive needs trend, not a snapshot" - the same principle that
already makes OI buildup and velocity valuable, applied to three more
signal categories that had no storage at all (computed fresh on every
scan, thrown away).
- New ticker_daily_snapshot table (TA + fundamentals + insider, one row
  per ticker per day, UPSERT on re-run) and job_run_log table (one
  place every scheduled job can log to - "did job X run today" is now
  one query, not five different ad-hoc checks). Both created ad-hoc
  against the live DB, same as this session's other schema changes -
  see Later.
- New app/signals/after_hours_batch.py::run_after_hours_batch(user_id) -
  loops get_scan_universe(), resilient per-ticker (one failure doesn't
  abort the other 130), scheduled alongside velocity_snapshot (4:15pm
  ET). Also closes the iv_history gap: _record_iv_history() previously
  only ran for tickers incidentally enriched during a manual scan - now
  every watchlist ticker gets a row, every day.
- Verified live end-to-end, not just import-checked: real run reached
  131/131 tickers (0 failed), job_run_log got a real row, sample
  ticker_daily_snapshot rows have genuinely varied (not fallback-stuck)
  TA/fundamentals, and iv_history now has fresh rows for tickers that
  were never part of any manual scan today.
- **get_insider_activity() (signals/edgar_insider.py) fixed - four
  stacked bugs, found via direct EDGAR API testing after a live
  131-ticker run showed 0/131 tickers with ANY insider signal (not even
  routine sells, implausible if this worked at all):
  1. The search URL's date filter (dateRange=custom&startdt=... with no
     enddt) didn't actually scope results - a "last 5 days" query
     returned hits from 2003. Fixed: added enddt=today.
  2. Every hit was then read via src.get("accession_no","") - that
     field doesn't exist in EDGAR's response at all (the real field is
     "adsh"), so `if not accession: continue` silently dropped every
     hit regardless of #1. This function had likely never once
     returned a real transaction. Fixed: read "adsh".
  3. The old _parse_form4 then guessed a filing's XML filename via a
     second "fetch an index page, look for a .xml" round-trip, using
     accession_no[:10] as a stand-in for CIK - that's the filing
     AGENT's CIK, not the issuer's, and never resolves. Fixed: the
     search hit's own "_id" field already contains the exact primary
     document filename, and "_source.ciks" already contains the
     issuer's CIK (last element) - one direct fetch, no guessing,
     verified against a live Apple filing.
  4. Ticker-match verification and C-suite detection were both reading
     search-hit fields that don't exist either (display_names' format
     changed over time and doesn't reliably carry the ticker in current
     filings; entity_name never existed). Fixed: both now read directly
     from the filing's own XML (issuerTradingSymbol for ticker match;
     isOfficer/isDirector/officerTitle for C-suite, isDirector added
     since the original CSUITE keyword set explicitly includes bare
     "director"/"chairman", not just executive titles).
  Verified live end-to-end, not just in isolation: re-ran the full
  131-ticker batch after fixing - insider signal distribution went from
  120 NEUTRAL/0 BEARISH/0 real transactions (100% silently broken) to
  120 NEUTRAL/11 BEARISH with real dollar values, real names (e.g.
  Apple CFO Kevan Parekh, Chairman Arthur Levinson both correctly
  flagged csuite=True), and one confirmed C-suite sell (CRWD). Also
  affects smart_stock_scan.py's existing insider-weighted composite
  score (25% of its weighting had effectively been a no-op).

---

## Recommended Priority Order

Given everything above, here's what I'd actually tackle next, in order,
and why:

1. **check_fills() contract-matching bug** (new, see Immediate below) -
   already caused one real mislabeled fill (MSTR) on a live account.
   Will happen again to anyone with existing broker positions the
   moment a scan pick shares a ticker. Concrete, already-manifested,
   cheap to fix properly.

2. **Returning-user login path** (new, see Immediate below) - a real
   product blocker for the next milestone (private beta): right now
   nobody who's already redeemed an invite can log back in without a
   manual database reset. This needs to exist before inviting real
   test users.

3. **MCP key regeneration** - directly related to #2 in spirit (both
   are "can an existing customer actually get back in"). Small, and
   create_api_key() already supports it.

4. **Hosted MCP server deployment** - multi-tenant MCP access is fully
   built and tested but unreachable by anyone outside your own machine.
   This is the one item standing between "built" and "a real customer
   can actually use Claude Desktop with their key."

5. **Predictive IV-expansion signal** - queued since earlier this
   session, genuinely still open, a real recommendation-quality
   upgrade once the above product-readiness items are settled.

Everything else in Next Up / Later / Future is lower-urgency and
doesn't block the above.

---

## Immediate / In Progress

- [ ] **check_fills() matches on bare ticker symbol, not real contract
      details.** Auto-detects fills by comparing your live Webull
      positions against today's active recommendations - but only
      compares the underlying ticker, not strike/expiry/type. If you
      already own ANY position on a ticker that also appears in
      today's scan, it gets silently marked as filling that specific
      recommendation, using the existing position's price/qty. Caught
      live this session: an existing MSTR options position got
      auto-matched to an unrelated NAKED_CALL recommendation on the
      same ticker. Cleaned up the resulting bad DB rows (both
      daily_recommendations and the tracked_positions row it created);
      the root cause in check_fills() itself is not yet fixed.
- [ ] **No login path for returning users, separate from invite
      redemption.** login_with_invite() is the only way in - it
      requires a still-pending invite_code every time. Once an invite
      flips to accepted, the only way to get back into that same
      account is a manual UPDATE invites SET status='pending' in the
      database. Fine for one admin doing manual testing; a real
      blocker before inviting actual beta users, who have no database
      access.
- [ ] tracked_positions.daily_rec_id - column exists in schema, not
      yet populated by the current fill-tracking flow (which matches
      by ticker + fill-price proximity instead). A more direct link;
      low priority, current approach works and is tested.

---

## Next Up

- [ ] Predictive IV-expansion signal - iv_history table exists
      (per-ticker IV snapshots over time) but nothing reads from it
      yet. Goal: catch IV expanding ahead of a price move, distinct
      from OI buildup and from momentum (inherently reactive). Paired
      with a simple anti-chasing rule: don't recommend a ticker that's
      already moved >=5% today in either direction.
- [ ] MCP key regeneration - customer keys are minted once at account
      creation with no way to get a new one if lost; needs a self-serve
      endpoint (create_api_key() already supports multiple active keys
      per user).
- [ ] Hosted MCP server - MCP_TRANSPORT=http + ApiKeyTokenVerifier exist
      and are wired in, but nothing is actually deployed/reachable yet;
      blocked on the cloud deployment item below.
- [ ] monitor_config table - schema exists with plausible columns
      (is_active, check_interval, total_alerts_fired) but usage by
      position_monitor.py not yet confirmed either way.
- [ ] ChromaDB - running in docker-compose.yml, zero imports anywhere
      in the codebase. Either remove the container, or wire it into
      real RAG instead of context_builder.py's current direct-API-call
      approach.
- [ ] ETF scoring in stock scans - currently a technicals-only
      stand-in since ETFs don't have analyst targets/PEG/revenue-
      growth. Works, but a real fund-appropriate model (expense ratio,
      index-tracking quality) would be better.
- [ ] UW underutilization - economic calendar and net-flow-by-expiry
      are never called; both are plausible real signal upgrades.
      Congressional trades and insider-ownership-pct tools exist but
      don't feed conviction scoring or the LLM prompt.
- [ ] Real closed trades - conviction-weight recalibration, all 3
      backtests, and strategy-performance analysis all need actual
      closed trades to activate. Plan discussed this session: once the
      engine is solid, automate this via paper trading - a cron job
      that buys at the recommended price and closes at end of day,
      generating real outcome data without real money at risk.
- [ ] conviction.py cleanup - orphaned this session (only caller, the
      old daily_engine.py path, was retired); no remaining importers.
      Either delete it or repurpose it.
- [ ] 3m "both" horizon's stock half still uses horizon_engine.py's
      naive per-ticker loop, not smart_stock_scan.py's composite
      pre-filter (unlike pure 6m/1yr stock scans). Narrow, not urgent.

---

## Later

- [ ] Real Robinhood/IBKR/Tastytrade implementations - factory.py's
      abstraction and SnapTrade plumbing are ready; the actual
      OAuth/positions/orders code for each isn't written yet
- [ ] Versioned migrations for schema changes applied ad-hoc this
      session - currently only exist as commands run directly against
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
- [ ] Cloud deployment (Cloudflare tunnel or Railway.app) - this now
      directly unblocks the Hosted MCP server item above, so it's
      more load-bearing than a pure "future" item at this point
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
- FastAPI wrapper on :8001; MCP supports both stdio (local admin) and
  HTTP (hosted, multi-customer via ApiKeyTokenVerifier) via the
  MCP_TRANSPORT setting
- Invite code auth (existing DB system, no OAuth needed yet) - but see
  Immediate: no path back in once an invite is accepted
- user_watchlist + is_admin flag is the entire watchlist system - no
  separate default-watchlist table, no broker dependency for scanning
- No hardcoded fallback ticker lists anywhere - an empty, honest result
  beats a silent, confusing substitute
- strategy_recommendations retired - daily_recommendations is the
  single source of truth for both recommendations AND fill/outcome
  tracking
- watchlist_sync is add-only - never deletes a manually-added ticker
  just because a connected broker doesn't have it
- One scan engine per job, no fallback to a second implementation -
  rescan_engine.py for daily options recs (MCP and web both),
  smart_stock_scan.py for watchlist-wide stock scans (MCP and web
  both) - a failure surfaces as a real error, not a silent degrade to
  a different, possibly-disagreeing engine
- Portfolio (live Webull view) is admin-only by design, both backend
  and frontend - every other user's equivalent is their own confirmed-
  filled recommendations, via the open-positions endpoint
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