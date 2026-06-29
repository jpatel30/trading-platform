# MCP Tools Reference

All tools available via Claude Desktop MCP integration.
Server: `python3 -m app.mcp_server.server`

Total: **48 tools** across 8 categories.

---

## Portfolio

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| `ping` | — | status, uptime, version | Health check — run first |
| `get_positions` | — | list of positions | Live from Webull |
| `get_balances` | — | net_liq, cash, buying_power | Live from Webull |
| `get_orders` | — | today's orders | Live from Webull |
| `get_active_bets` | — | positions enriched with target/stop/status | Rule-based |
| `get_portfolio_pnl` | — | total P&L, win rate, best/worst | Calculated |

## Market Data

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| `get_market_status` | — | open/closed, last trading date | |
| `get_quote` | `ticker` | price, change, volume | UW stock_state (live) |
| `get_quotes` | `tickers: list` | dict of quotes | Batch |
| `get_price_history` | `ticker, days=200, timespan="day"` | OHLCV bars | UW OHLC |
| `get_ticker_info` | `ticker` | name, market cap, sector | |
| `analyze_ticker` | `ticker, days=300` | full TA profile | MA, RSI, MACD, trend |
| `get_market_overview` | — | VIX, market tide, sector flow | |
| `get_market_context` | `ticker` | full RAG context | Used by LLM |

## Options Flow

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| `get_options_flow` | `ticker=None, limit=50` | unusual sweeps | UW flow alerts |
| `get_dark_pool` | `ticker=None, min_premium=0` | block trades | UW dark pool |
| `get_gex` | `ticker` | gamma exposure by strike | UW GEX |
| `get_ticker_signal` | `ticker` | combined flow + dp score + direction | |

## News & Calendar

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| `get_earnings_calendar` | — | today pre/post market earnings | UW |
| `get_news` | `ticker=None` | headlines with sentiment | UW |
| `get_congress_trades` | `ticker=None` | recent congressional trades | UW |

## Recommendations

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| `get_daily_recommendations` | `force_refresh=False` | cached or fresh recs | DB cache |
| `invalidate_recommendation` | `ticker, reason` | success bool | Marks stale |
| `get_recommendation_history` | `days_back=7` | past recs with outcomes | |
| `get_horizon_recommendation` | `ticker, horizon="1m", budget=2000` | single-ticker, single-horizon | |
| `scan_for_horizon` | `horizon="1m", budget=2000, top_n=5` | top picks for one horizon | |
| `get_strategy_recommendation` | `ticker, direction, budget, expiry=None` | full trade plan | LLM |
| `get_stock_recommendation` | `ticker, horizon="6m", budget=2000` | stock thesis + target | |
| `get_portfolio_additions` | — | what to add given current portfolio | |
| `run_backtest` | — | historical accuracy of past recs | |

## Watchlist & Scanner

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| `get_watchlist` | — | 127-ticker list from DB | |
| `get_scan_universe` | — | full 127-ticker scan universe | |
| `force_sync_watchlist` | — | syncs Webull → DB | |
| `add_to_watchlist` | `ticker` | success | |
| `remove_from_watchlist` | `ticker` | success | |

## Position Monitor & Alerts

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| `start_monitor` | — | status | Polls every 2 min |
| `stop_monitor` | — | status | |
| `get_monitor_status` | — | running, last_check, pending_alerts | |
| `get_active_alerts` | `limit=20` | unread alerts sorted by urgency | |
| `dismiss_alert` | `alert_id` | success | |
| `dismiss_all_alerts` | — | count dismissed | |
| `get_sell_signals` | — | positions triggering exit rules | Rule-based |
| `get_sell_signal_compliance` | — | how often you act on signals | Learning |
| `mute_alerts` | `ticker=None` | mutes globally or per ticker | |
| `unmute_alerts` | `ticker=None` | re-enables alerts | |
| `get_mute_status` | — | what's currently muted | |

## Execution Tracking

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| `confirm_execution` | `symbol, entry_price, qty` | tracked_position created | After you trade |
| `log_outcome` | `symbol, exit_price, exit_reason` | learning_log updated | After you close |
| `get_trade_history` | — | all confirmed trades + outcomes | |
| `mark_sell_acted` | `symbol` | compliance logged | |

## Notifications & Learning

| Tool | Parameters | Returns | Notes |
|---|---|---|---|
| `configure_discord` | `webhook_url` | test result | |
| `test_notification` | — | sends test to Discord | |
| `get_notification_config` | — | current Discord config | |
| `get_learning_report` | — | accuracy, action items, patterns | |
| `get_strategy_weights` | — | current conviction factor weights | |

---

## Usage Examples

```
"ping"
→ All systems green. DB ✅ UW ✅ Ollama ✅ Webull ✅

"get active bets"
→ 21 positions. Winners: INTC +162%, WDC +112%, SNDK +270%...
  Sell signals: GLD option -94% → CLOSE IMMEDIATELY

"get daily recommendations"
→ Checking cache... 2 recs from today's scan:
  FIG BULLISH DEBIT_CALL_SPREAD exp=Jul17 conf=70/100
  SNOW BULLISH DEBIT_CALL_SPREAD exp=Jul17 conf=75/100

"scan for horizon 1w budget 1000"
→ Running 1-week scan... NVDA BEARISH DTE=7 PUT_SPREAD $195/$185

"get sell signals"
→ GLD option: -94.8% — CLOSE (loss too large, no recovery thesis)
  GLD option: -60.1% — REVIEW (approaching -65% stop)

"I bought 2 FIG $16c 7/17 at $0.85"
→ Confirmed. Tracking FIG: entry $0.85×200 = $170. Target $0.34, Stop $0.51
  Alert set for +100% ($1.70) and -40% ($0.51)
```

---

## Horizon Reference

| Horizon | Type | DTE Range | Min Conviction |
|---|---|---|---|
| `1w` | Options | 5-9 DTE | 75/100 |
| `2w` | Options | 10-16 DTE | 72/100 |
| `1m` | Options | 21-35 DTE | 70/100 |
| `3m` | Options or Stock | 60-90 DTE | 65/100 |
| `6m` | Stock | N/A | 60/100 |
| `1yr` | Stock | N/A | 55/100 |

---

## No-Watchlist Behavior

```
Options → SPY and QQQ only
Stocks  → Prompt: "Give up to 3 criteria: sector / market cap / theme"
           Example: "tech sector, large cap, AI theme"
```
