# Remaining Items

Last updated: June 2026

---

## 🔴 Monday (Must Complete Sunday Night)

- [x] P1: Fix CursorResult bug (DB store working)
- [x] P2: UW replaces Polygon for bars/price (uw_market_data.py)
- [x] P3: Add biweekly (2w) horizon
- [x] P4: Single-page dashboard (StockBros)
- [x] P5: Clean test data (position_alerts, sell_recs, tracked_positions)
- [x] Smart engine: 1 LLM call for all horizons (26s)
- [x] Batch UW flow signals (2 calls not 254)
- [x] Fix TA engine UW key normalization (c→close)
- [ ] **Run cleanup_p5.sql against production DB**
- [ ] **Monday checklist: `bash RUNBOOK.sh checklist`**
- [ ] **First trade: execute highest conviction rec ≥70/100**

---

## 🟡 This Week (Post First Trade)

### Recommendations
- [ ] End-of-day learning: check if recommendation direction was correct
  - Auto-compare daily_recommendations vs actual price moves
  - Update learning_log with was_correct + pnl_if_acted
  - Surface accuracy per horizon (1w vs 1m vs 3m)
- [ ] Stock LLM thesis: add Ollama call to generate 2-sentence thesis
  - Currently returns fundamentals only (no narrative)
- [ ] No-watchlist stock flow: ask for ≤3 filter criteria (sector/cap/theme)
  - Currently defaults to NVDA/AAPL/MSFT hardcoded
- [ ] Add 6m/1yr to options horizons in smart engine
  - Currently only 1w/2w/1m/3m

### Dashboard
- [ ] Show confidence score on recommendation cards (currently conf=?)
- [ ] Add "confirm trade" button on recommendation card
  - Calls `POST /api/execution/confirm`
- [ ] Learning page: show recommendation accuracy by horizon
- [ ] Sell signals visible in main portfolio panel

### MCP Server
- [ ] Add `run_smart_scan` MCP tool (calls smart_engine directly)
  - Replaces calling daily_recommendations for full run
- [ ] Add `get_institutional_ownership` MCP tool
- [ ] Update `get_horizon_recommendation` to use smart_engine

---

## 🟢 This Month (Growth)

### Open Source Prep
- [ ] Create `.env.example` (no real keys)
- [ ] Update Docker Compose for easy setup
- [ ] Fix requirements.txt typo (`black>=24.0%` → `black>=24.0`)
- [ ] Remove any hardcoded user IDs
- [ ] Public README with setup video

### Multi-Broker
- [ ] RobinhoodBroker implementation (most requested)
  - Stub exists in app/broker/factory.py
  - Need: OAuth flow + positions/orders endpoints
- [ ] IBKR planned (quarterly)
- [ ] Tastytrade planned (quarterly)

### Cloud Deployment
- [ ] Cloudflare tunnel setup (AMD Windows machine)
  - cloudflared pod → routes traffic to Mac
  - app.yourdomain.com → localhost:3000
- [ ] Or Railway.app if Cloudflare too complex
  - Switch LLM: Ollama → claude-haiku-4-5 (~$9/mo vs free)
  - Railway Postgres ($5/mo) replaces Docker Postgres

### Platform Intelligence
- [ ] Seasonal patterns: add UW `/api/seasonality/{ticker}/monthly`
  - "NVDA averages +8% in July historically"
- [ ] Earnings call transcripts: `/api/companies/{ticker}/transcripts/{quarter}`
  - Feed into LLM context for 1m/3m recommendations
- [ ] Short interest: `/api/shorts/{ticker}/interest-float/v2`
  - Short squeeze potential in conviction scoring
- [ ] Annual UW upgrade: 40K requests/day (vs 20K now)
  - Enables more enrichment per scan without 429s

---

## 🔵 Future (When Monetizing)

### StockBros Platform
- [ ] Polygon/Massive Starter ($29/mo) — real-time all-ticker snapshot
  - Scanner goes from 1.2s with yesterday's close → 0.5s with live prices
- [ ] Multi-user onboarding flow (invite code → account creation)
- [ ] User-specific watchlists (currently JP's 127 tickers for all)
- [ ] Recommendation sharing between users
- [ ] Public invite page (stockbros.app)

### Advanced Features
- [ ] WebSocket real-time dashboard updates (UW annual plan)
- [ ] Mobile push notifications (PWA service worker)
- [ ] Automatic trade execution (requires Webull trading API approval)
- [ ] Portfolio rebalancing recommendations
- [ ] Options expiry calendar view

---

## Data Quality Notes

```
Current weekend/holiday behavior:
  Flow signals: batch returns 0 for most tickers (market closed)
  TA signals: based on Friday close — reliable
  IV rank: real from UW (not affected by market hours)
  Recommendation: shifts to momentum + TA only (flow weight reduced)

This is correct behavior. On market days:
  Flow: 50+ alerts with real sweeps
  Dark pool: 100+ prints with direction
  Recommendation quality improves significantly
```

---

## Architecture Decisions Locked

```
✅ UW for OHLC bars (not Polygon — 87x faster)
✅ Polygon grouped_daily for scanner prices (1 call, no UW equivalent)
✅ yfinance for VIX (UW VIX = 403 on monthly plan)
✅ Ollama local for LLM (cloud = Cloudflare/Railway with haiku)
✅ Separate repos: trading-platform (open) vs stockbros (consumer)
✅ FastAPI wrapper on :8001 (MCP stays stdio, not HTTP)
✅ Single-page dashboard (no page navigation needed)
✅ Invite code auth (existing DB system, no OAuth needed yet)
```
