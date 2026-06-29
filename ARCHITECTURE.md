# Trading Intelligence Platform — Architecture

**Last updated:** June 2026

---

## System Overview

```
User (Claude Desktop / StockBros Browser)
         │
         ├── Claude Desktop ──→ MCP Server (stdio, port N/A)
         │                          │
         └── Browser ──────→ FastAPI :8001 ──┤
                                              │
                              ┌───────────────▼──────────────────┐
                              │         Core Engine               │
                              │                                   │
                              │  Scanner → Smart Engine → LLM    │
                              │                                   │
                              │  Data: UW + Polygon + yfinance   │
                              └───────────────┬──────────────────┘
                                              │
                              ┌───────────────▼──────────────────┐
                              │           PostgreSQL               │
                              │     (Docker, localhost:5432)      │
                              └──────────────────────────────────┘
```

---

## Data Flow — Daily Recommendation Run

```
07:00 AM  Start
          │
          ├─ Pre-fetch (shared, once):
          │   VIX (yfinance)                    0.3s
          │   Global news (UW)                  0.2s
          │   Earnings calendar (UW)            0.2s
          │   Batch flow alerts (UW, limit=500) 0.2s
          │   Batch dark pool (UW, limit=500)   0.1s
          │
          ├─ Scanner (127 tickers):
          │   Polygon grouped_daily (1 call)    2.0s
          │   UW flow grouping (in-memory)      0.0s
          │   Score + rank picks                0.5s
          │   → Top 15 picks
          │
          ├─ Enrich candidates (parallel, 8 threads):
          │   Per ticker: IV rank + OHLC + expiries + news
          │   8 tickers × 4 UW calls = 32 calls
          │   All parallel → max(0.15s each)    ~1.0s
          │
          ├─ Single LLM call (Ollama Qwen 14B):
          │   Input: 10 compressed ticker contexts (~3500 tokens)
          │   + Market context (VIX, news, earnings)
          │   + All available expiries per ticker
          │   LLM decides: ticker + expiry + strategy + strikes
          │                                    ~16-18s
          │
          ├─ Deterministic math:
          │   Validate strikes vs real UW contracts
          │   Calculate cost / max profit / max loss / R:R
          │   Apply position sizing                1s
          │
          └─ Store + Alert:
              daily_recommendations table
              Discord notification if act_now=True
```

---

## Component Map

```
app/
├── api/
│   └── main.py              FastAPI REST wrapper (:8001) — 20 endpoints
│                            Called by StockBros dashboard
│
├── broker/
│   ├── webull_connector.py  Webull OpenAPI client (positions, orders, balance)
│   ├── factory.py           Multi-broker abstraction (Webull now, Robinhood planned)
│   ├── sell_signals.py      Rule-based exit signals (-40/-50/-100% triggers)
│   ├── active_bets.py       Enrich positions with target/stop/status
│   └── watchlist_sync.py    Sync Webull watchlist → DB
│
├── scanner/
│   └── quick_scan.py        127-ticker scanner
│                            Prices: Polygon grouped_daily (1 call)
│                            Flow: UW batch (2 calls, not 254)
│                            Output: ranked picks with direction + signals
│
├── recommendations/
│   ├── smart_engine.py      NEW: multi-horizon engine (1 LLM call)
│   │                        Phase 1: parallel enrichment
│   │                        Phase 2: single LLM call
│   │                        Phase 3: deterministic math
│   ├── daily_engine.py      Daily recommendations orchestrator
│   ├── horizon_engine.py    Per-horizon options/stock logic
│   │                        Horizons: 1w, 2w, 1m, 3m, 6m, 1yr
│   ├── conviction.py        12-factor conviction scoring (0-100)
│   └── fundamentals.py      yfinance fundamentals + analyst targets
│
├── strategy/
│   └── engine.py            Trade math: strike selection, cost, R:R
│                            LLM decides strategy type
│                            Python executes all arithmetic
│
├── options_flow/
│   └── unusual_whales.py    All UW API calls
│                            get_ohlc, get_stock_state, get_iv_rank (NEW)
│                            get_flow_alerts, get_dark_pool_recent
│                            get_institutional_ownership (NEW)
│                            get_greek_flow, get_net_premium_ticks (NEW)
│                            get_etf_sector_flow (NEW)
│
├── market_data/
│   ├── uw_market_data.py    Drop-in replacement for polygon_client
│   │                        get_bars() → UW OHLC first, Polygon fallback
│   │                        get_previous_close() → UW stock_state (live)
│   └── polygon_client.py    Polygon/Massive API
│                            get_grouped_daily() — kept (all-ticker batch)
│
├── technical_analysis/
│   └── engine.py            MA20/50/200, EMA9/21, RSI14, MACD
│                            Handles UW short keys (c/o/h/l) automatically
│
├── rag/
│   └── context_builder.py   Builds rich context for LLM calls
│                            Price + IV + macro + news + geopolitical
│
├── monitor/
│   └── position_monitor.py  Background monitor (polls every 2 min)
│                            Fires alerts: +50/100/200/500%, -40% stop
│                            Caches portfolio for instant dashboard load
│
├── learning/
│   └── engine.py            Tracks recommendation accuracy
│                            End-of-day: did we make money?
│                            Adjusts conviction weights over time
│
├── mcp_server/
│   └── server.py            48 MCP tools for Claude Desktop
│                            See MCP_TOOLS.md for full list
│
├── api/
│   └── main.py              FastAPI REST API for StockBros dashboard
│                            JWT auth via invite codes
│                            20 endpoints covering all platform features
│
└── db/
    ├── session.py           SQLAlchemy session management
    └── migrations/          Schema migrations
```

---

## Database Schema (19 tables)

### Auth & Users
```
users               id, email, display_name, invited_by, is_active
user_profiles       user_id, risk_tolerance, conviction_weights
user_api_keys       user_id, service, encrypted_key
invites             id, invited_by, email, invite_code, status, expires_at
broker_connections  user_id, broker_name, auth_method, encrypted_tokens, is_active
```

### Watchlist & Config
```
user_watchlist      user_id, ticker                    (127 tickers for JP)
monitor_config      user_id, alert_thresholds
muted_symbols       user_id, ticker, muted_until
notification_config user_id, discord_webhook
```

### Recommendations
```
daily_recommendations  user_id, ticker, date, horizon, direction,
                       conviction_score, conviction_tier, thesis,
                       entry_zone_low/high, target_price/pct, stop_price/pct,
                       strategy, expiry, legs, key_news, status

strategy_recommendations  user_id, symbol, actual_entry, contracts,
                          expiry, strategy, rec_date, status

sell_recommendations  position_id, ticker, signal_type, trigger_pct
```

### Tracking & Learning
```
tracked_positions   user_id, ticker, entry_price, qty, expiry
position_alerts     user_id, symbol, alert_type, urgency, message, is_read
iv_history          ticker, date, atm_iv, iv_rank (builds real 1-year rank)
learning_log        rec_id, outcome, pnl, was_correct, horizon
news_impact_log     ticker, headline, actual_move, predicted_direction
portfolio_cache     user_id, positions, balances, cached_at (for instant load)
notification_log    user_id, channel, message, sent_at
```

### Key Relationships
```
users ──< broker_connections (one user, multiple brokers)
users ──< user_watchlist     (127 tickers per user)
users ──< daily_recommendations (all recs for this user)
users ──< tracked_positions  (confirmed trades)
daily_recommendations ──< tracked_positions (when user confirms execution)
tracked_positions ──< learning_log (outcome tracking)
```

---

## Conviction Scoring (12 factors, 0-100)

```
Factor              Weight  Source
──────────────────────────────────────────────
entry_trigger       20%     TA (AT_SUPPORT/AT_RESISTANCE/BREAKOUT)
options_flow        20%     UW flow alerts + dark pool (hard block if contradicts)
iv_rank             15%     UW real 1-year IV rank
ta_alignment        20%     MA20/50/200 + RSI + MACD trend
vix_zone            15%     yfinance VIX (LOW/NORMAL/HIGH/EXTREME)
volume              10%     vs 20-day average
──────────────────────────────────────────────
UW bonus signals (+0 to +10 additive):
  net_premium_ticks  +5     call vs put net premium confirms direction
  greek_flow         +3     call vs put gamma direction confirms
  institutional      +2     >70% institutional ownership score

Tiers:
  80-100: VERY_HIGH  → strong green, act_now=True
  70-79:  HIGH       → green, act_now=True
  55-69:  MODERATE   → yellow, watch
  40-54:  WATCH      → orange, skip
  <40:    SKIP       → red, skip
```

---

## Integration Endpoints

### Unusual Whales (paid, 120 req/min, 20K/day)
```
/api/stock/{ticker}/ohlc/{size}           Daily/intraday OHLC bars
/api/stock/{ticker}/stock-state           Live price including pre/post market
/api/stock/{ticker}/iv-rank              Real 1-year IV rank
/api/stock/{ticker}/expiry-breakdown     Available expiry dates
/api/stock/{ticker}/option-contracts     Options chain with IV
/api/stock/{ticker}/greek-exposure       GEX by strike
/api/stock/{ticker}/greek-flow           Call vs put gamma direction
/api/stock/{ticker}/net-prem-ticks       Call vs put net premium
/api/option-trades/flow-alerts           Unusual sweeps (batch, all tickers)
/api/darkpool/recent                     Dark pool prints (batch, all tickers)
/api/darkpool/{ticker}                   Per-ticker dark pool
/api/institution/{ticker}/ownership      Institutional ownership %
/api/etfs/{ticker}/in-outflow           Sector ETF money flow
/api/earnings/premarket                  Earnings today premarket
/api/earnings/afterhours                 Earnings today after hours
/api/earnings/{ticker}                   Historical earnings + expected move
/api/news/headlines                      News with sentiment
/api/congress/recent-trades              Congressional trades
/api/insider/{ticker}/ticker-flow       Insider transactions
/api/market/market-tide                  Aggregate market bull/bear
```

### Polygon/Massive (free tier)
```
/v2/aggs/grouped/locale/us/market/stocks/{date}  All-ticker daily OHLC (1 call)
```

### Webull OpenAPI
```
/v1/account/positions    Open positions
/v1/account/balance      Account balance + net liquidation value
/v1/account/orders       Today's orders
/v1/user/watchlist       Watchlist sync
```

---

## Performance Targets

| Operation | Target | Actual |
|---|---|---|
| Scanner (127 tickers) | <5s | 1.2s ✅ |
| Smart recommendations | <30s | 26s ✅ |
| Portfolio load (cached) | <1s | <1s ✅ |
| Portfolio load (live) | <5s | 3-5s ✅ |
| Alert detection | <2min | 2min ✅ |
| End-of-day learning | background | background ✅ |

---

## StockBros Dashboard (stockbros repo)

```
Next.js 14 + Tailwind + mobile-responsive PWA
Runs on :3000, calls trading-platform FastAPI on :8001

Single-page layout:
  ┌─ Portfolio strip (always visible: net liq, P&L, cash, win rate) ─┐
  │ Positions (left) │ Options Picks (center)  │ Alerts (right)      │
  │ Winners/Losers   │ Daily input form         │ Position alerts     │
  │ Sell signals     │ → Scan → Results         │ Sell signals        │
  └─────────────────────────────────────────────────────────────────┘

Auth: invite code → JWT (30 days)
```
