# MCP Tools Reference

All tools available via Claude Desktop MCP integration.
Server: python3 -m app.mcp_server.server

Total: 61 tools across 9 categories.

---

## Portfolio

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| ping | - | DB connection status | Health check - run first |
| get_positions | - | list of positions | Live from Webull |
| get_balances | - | net_liq, cash, buying_power | Live from Webull |
| get_orders | - | today's orders | Live from Webull |
| get_active_bets | - | positions enriched with target/stop/status | Rule-based |
| get_portfolio_pnl | - | total P&L, win rate, best/worst | Calculated |

## Market Data

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| get_market_status | - | open/closed, last trading date | |
| get_quote | ticker | price, change, volume | |
| get_quotes | tickers: list | dict of quotes | Batch |
| get_price_history | ticker, days=200, timespan="day" | OHLCV bars | |
| get_ticker_info | ticker | name, market cap, sector | |
| analyze_ticker | ticker, days=300 | full TA profile | MA, RSI, MACD, trend |
| get_market_overview | - | VIX, market tide, sector flow | |
| get_market_regime | - | overall bias + strategy hint from VIX term structure + put/call ratio | Contrarian PCR read + VIX9D-vs-VIX30 inversion check |
| get_market_context | ticker | full RAG context (price, earnings, macro, news, sector) | Used by LLM; also useful standalone for research |

## Options Flow

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| get_options_flow | ticker=None, min_premium=500000, sweeps_only=False | unusual sweeps | |
| get_dark_pool | ticker=None, min_premium=0 | block trades | |
| get_gex | ticker | gamma exposure by strike/expiry | |
| get_oi_buildup | ticker | OI buildup score -100 to +100, days building, top contract | LEADING indicator (multi-day accumulation) distinct from same-day flow (reactive) |
| get_ticker_signal | ticker | combined flow + dp + direction + confidence | Call before get_strategy_recommendation |

## News & Calendar

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| get_earnings_calendar | - | today pre/post market earnings | |
| get_news | ticker=None | headlines with sentiment | |
| get_congress_trades | ticker=None | recent congressional trades | Not yet fed into conviction scoring |

## Recommendations

Five tools return a recommendation; they differ in scope, not
purpose - use this to pick the right one:

```
Single ticker, options only, deep dive     -> get_strategy_recommendation
Single ticker, options OR stock by horizon -> get_horizon_recommendation
Single ticker, stock only (3m/6m/1yr)      -> get_stock_recommendation
Scan whole watchlist, options + conviction -> get_daily_recommendations
Scan whole watchlist, routed by horizon    -> scan_for_horizon
```

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| get_daily_recommendations | force_refresh=False, budget=2000.0, trading_window_days=7, stop_loss_pct=40.0, profit_target_pct=50.0 | cached or fresh recs, gated >=70/100 | Main daily entry point. Runs the exact same engine (rescan_engine.py/smart_engine.py) as the web dashboard's Scan button - no separate MCP-only scoring path |
| invalidate_recommendation | ticker, reason | success bool | Fires Discord alert |
| get_recommendation_history | days_back=7 | past recs with status | |
| get_recommendation_history_detailed | days_back=30 | history grouped by date, with mark-to-market P&L | Answers "how have my recommendations performed?" |
| get_horizon_recommendation | ticker, horizon="1m", budget=2000 | single-ticker, single-horizon, options or stock | |
| scan_for_horizon | horizon="1m", budget=2000, top_n=5 | top picks for one horizon, watchlist-only universe | Stock horizons (6m/1yr) use the same fundamentals+velocity+insider composite pre-filter as the web dashboard's stock scan (smart_stock_scan.py) - previously a weaker, unfiltered per-ticker loop |
| get_strategy_recommendation | ticker, budget, max_loss=None, profit_target=None, min_dte=4, max_dte=365 | full options trade plan | Single-ticker deep dive ONLY - use get_daily_recommendations for the gated daily flow |
| get_stock_recommendation | ticker, horizon="6m", budget=2000 | stock thesis + target | |
| get_portfolio_additions | - | existing winners worth adding to | |
| run_backtest | - | all 3 backtests (sell-signal cost, entry quality, conviction gate) | |
| get_backtest_stats | days_back=90 | win rate by conviction tier / strategy / horizon | Excludes rows flagged excluded_from_stats (corrupted historical data) |

## Watchlist & Scanner

get_watchlist (your list) and get_scan_universe (what actually gets
scanned) can differ if you're not the admin, or if you've chosen
default_only mode - they answer different questions, not the same one.

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| get_watchlist | - | your tickers, DB source, sync status | Reads via watchlist_sync - add-only mirror from broker if connected |
| get_scan_universe | extra_tickers=None, min_market_cap=0, sectors=None, min_price=0 | the actual universe a scan will use | Admin's shared default + your own additions, no broker dependency, no hardcoded fallback |
| force_sync_watchlist | - | immediate broker-to-DB sync diff | Add-only - never deletes from your DB list |
| add_to_watchlist | ticker, notes="", sector="" | success | |
| remove_from_watchlist | ticker | success | |
| scan_watchlist | top_n=5 | raw two-tier convergence scan, NO conviction gate | Each pick includes a conflict flag (confidence capped at 58 when strong signals disagree). For filtered daily picks use get_daily_recommendations instead |

## Position Monitor & Alerts

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| start_monitor | - | status | Polls every 15 min; auto-resumes on server restart |
| stop_monitor | - | status | |
| get_monitor_status | - | running, last_check, pending_alerts | |
| get_active_alerts | limit=20 | unread alerts sorted by urgency | |
| dismiss_alert | alert_id | success | |
| dismiss_all_alerts | - | count dismissed | |
| get_sell_signals | use_llm=True | positions triggering exit rules | Rule-based tier + optional LLM batch analysis |
| get_sell_signal_compliance | - | how often you act on signals | Learning |
| mute_alerts | symbol=None, hours=None | mutes globally or per ticker | |
| unmute_alerts | symbol=None | re-enables alerts | |
| get_mute_status | - | what's currently muted | |

## Execution Tracking

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| confirm_execution | symbol, entry_price, qty, recommendation_id=None | tracked_position created with the REAL target/stop from the matching recommendation | Matches by ticker + fill-price proximity if recommendation_id omitted (handles same-day multi-strategy recs, e.g. SPY as both iron condor and debit spread) |
| log_outcome | symbol, exit_price, exit_reason="MANUAL" | actual P&L calculated, learning updated | Closes the daily_recommendations row too (ground-truth outcome, distinct from ongoing mark-to-market) |
| get_trade_history | limit=20 | all confirmed trades + outcomes | |
| mark_sell_acted | symbol, exit_pct=100 | compliance logged | |

## Notifications & Learning

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| configure_discord | webhook_url | test result | Per-user webhook, no shared/global fallback |
| test_notification | - | sends test to Discord | |
| get_notification_config | - | current Discord config | |
| get_nightly_learning_report | - | last automated nightly run: sell outcomes, backtest summary, weights recalibrated | The scheduled job itself, not the same as get_learning_report |
| get_learning_report | - | full report: compliance, strategy win rates, weight adjustments, action items | On-demand, broader than the nightly summary |
| get_strategy_weights | - | current conviction factor weights from your trade history | Needs 3+ closed trades to move off defaults |

---

## Usage Examples

```
"ping"
-> DB connected, user identified.

"get active bets"
-> Positions with target/stop status: TARGET_HIT / NEAR_TARGET /
   ON_TRACK / NEAR_STOP / STOP_HIT.

"get daily recommendations"
-> Checking cache... today's gated recs (>=70/100 conviction) with
   entry/target/stop and Webull instructions.

"scan for horizon 1w budget 1000"
-> Runs a fresh 1-week scan across your watchlist.

"get sell signals"
-> Rule-based + LLM-reviewed exit signals for every open position.

"I bought 2 NVDA calls at $8.50"
-> confirm_execution - pulls the REAL target/stop from today's NVDA
   recommendation (not a generic default), starts 15-min monitoring.
```

---

## Horizon Reference

| Horizon | Type | DTE Range | Min Conviction |
|---|---|---|---|
| 1w | Options | 5-9 DTE | 75/100 |
| 2w | Options | 10-16 DTE | 72/100 |
| 1m | Options | 21-35 DTE | 70/100 |
| 3m | Options or Stock | 60-90 DTE | 65/100 |
| 6m | Stock | N/A | 60/100 |
| 1yr | Stock | N/A | 55/100 |

---

## No-Watchlist Behavior

There is no hardcoded ticker fallback anywhere in the pipeline
anymore. If a user has no watchlist configured at all (no admin
default reachable, no personal additions), get_scan_universe returns
an honest empty list rather than substituting unrelated tickers -
this was a deliberate change this session, replacing an earlier
hardcoded 36-ticker fallback and a separate hardcoded NVDA/AAPL/MSFT
stock default.

---

## Known Gaps (tracked in REMAINING_ITEMS.md)

- Multi-user MCP identity resolution is fixed (per-request bearer-token
  auth via ApiKeyTokenVerifier, no process-level cache) and customer
  keys are minted automatically on signup - but there's no hosted
  server actually reachable yet (MCP_TRANSPORT=http needs a real
  deployment target) and no self-serve "regenerate a lost key" endpoint.