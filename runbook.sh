#!/bin/bash
# TRADING PLATFORM — RUNBOOK v3
# Usage: bash runbook.sh

cd ~/Documents/Claude/Projects/trading-platform
source venv/bin/activate

echo "=== SECTION 0: HEALTH CHECK ==="

python3 << 'PYEOF'
print('=== TRADING PLATFORM HEALTH CHECK ===')
errors = []

try:
    from app.db.session import get_session
    from sqlalchemy import text
    with get_session() as s: s.execute(text('SELECT 1'))
    print('✅ Postgres')
except Exception as e: print('❌ Postgres:', e); errors.append('postgres')

try:
    import requests
    r      = requests.get('http://localhost:11434/api/tags', timeout=3)
    models = [m['name'] for m in r.json().get('models', [])]
    ps     = requests.get('http://localhost:11434/api/ps', timeout=3).json()
    loaded = ps.get('models', [])
    if loaded:
        vram = loaded[0].get('size_vram', 0)
        gpu  = 'Metal GPU' if vram > 0 else 'CPU only'
        print(f'✅ Ollama: {models} | {gpu} | {round(vram/1e9,1)}GB VRAM')
    else:
        print(f'✅ Ollama: {models} | idle (loads on first call)')
except Exception as e: print('❌ Ollama:', e); errors.append('ollama')

try:
    from app.broker.webull_connector import WebullConnector
    from app.utils.current_user import get_current_user_id
    pos = WebullConnector(get_current_user_id()).get_positions()
    print(f'✅ Webull: {len(pos)} positions')
except Exception as e: print('❌ Webull:', e); errors.append('webull')

try:
    from app.broker.watchlist_sync import get_db_watchlist
    from app.utils.current_user import get_current_user_id
    t = get_db_watchlist(get_current_user_id())
    print(f'✅ Watchlist DB: {len(t)} tickers')
except Exception as e: print('❌ Watchlist:', e); errors.append('watchlist')

try:
    import requests
    from app.utils.config import settings
    r = requests.get('https://api.polygon.io/v2/aggs/ticker/NVDA/prev',
        params={'apiKey': settings.polygon_api_key}, timeout=5)
    price = r.json().get('results', [{}])[0].get('c', 0)
    print(f'✅ Polygon: NVDA prev close ${price}')
except Exception as e: print('❌ Polygon:', e); errors.append('polygon')

try:
    from app.options_flow.unusual_whales import get_flow_alerts
    get_flow_alerts(ticker='NVDA', limit=1)
    print('✅ UW Options Flow')
except Exception as e: print('❌ UW:', e); errors.append('uw')

try:
    from app.mcp_server.server import mcp
    print('✅ MCP Server')
except Exception as e: print('❌ MCP:', e); errors.append('mcp')

try:
    from app.rag.context_builder import build_ticker_context
    print('✅ RAG Pipeline')
except Exception as e: print('❌ RAG:', e); errors.append('rag')

try:
    from app.monitor.position_monitor import get_monitor
    print('✅ Position Monitor')
except Exception as e: print('❌ Monitor:', e); errors.append('monitor')

try:
    from app.broker.active_bets import get_active_bets
    print('✅ Active Bets')
except Exception as e: print('❌ Active Bets:', e); errors.append('active_bets')

print()
if errors: print('❌ Issues:', errors)
else:       print('✅ ALL SYSTEMS GO')
print('======================================')
PYEOF

echo ""
echo "=== SECTION 1: ALL CLAUDE DESKTOP COMMANDS ==="

cat << 'EOF'
----------------------------------------------------------
SYSTEM
  ping
  What is today's market status?

USE CASE 1 — PORTFOLIO
  Show me my full portfolio P&L
  What is my account balance and buying power?
  Show me today's orders

USE CASE 2 — WATCHLIST
  Show me my watchlist
  Sync my watchlist with Webull now
  Add TSLA to my watchlist
  Remove TSLA from my watchlist

USE CASE 3 — ACTIVE BETS (investment, profit target, stop loss)
  Show me my active bets with targets and stop losses
  Which positions have hit their stop loss?
  Which positions have hit their profit target?
  Which positions are near their stop loss?
  How many times have you recommended selling GLD that I ignored?

USE CASE 4 — BEST OPTIONS SUGGESTION
  Scan my watchlist and find the top picks today
  Give me a full options trade recommendation for NVDA with $2000 budget
  Give me a bearish trade on SPY with max loss $500
  Run the daily scan and give me top 10 picks instead of 5
  Give me a full recommendation for GOOGL with $3000 budget

USE CASE 5 — MONITOR PORTFOLIO
  Start monitoring my positions
  What is the monitor status?
  Show me my active alerts
  Dismiss all alerts
  Stop the position monitor

USE CASE 5b — MUTE / UNMUTE ALERTS
  Stop all alerts
  Stop alerts for GLD
  Mute GLD alerts for 24 hours
  Mute all alerts for 48 hours
  Resume alerts for GLD
  Resume all alerts
  What is currently muted?

USE CASE 6 — SELL SIGNALS
  Should I sell anything in my portfolio today?
  Run sell signals without LLM, just rule-based instant check
  What should I do with GLD?
  What should I do with SNDK at +287%?
  Show me past sell recommendations

USE CASE 7 — MONITOR OPTION RECOMMENDATIONS
  Show me my active bets with targets and stop losses
  Show me my active alerts
  Which positions have hit their profit target?
  Which positions are approaching their stop loss?

USE CASE 8 — NEW STOCKS TO ADD
  Scan my watchlist for stocks with strong momentum this week
  Show me the top converging picks from my watchlist today
  Give me market context for BE, should I add it to my portfolio?
  What is the sector momentum for semiconductors right now?

EXTRA — MARKET INTELLIGENCE
  Show me the full market overview and sector flow
  What is the options flow for NVDA?
  Show me dark pool trades for NVDA today
  What is the GEX for SPY this week?
  What companies report earnings this week?
  Show me recent market news
  Have any congress members traded NVDA recently?
  Which sectors are getting the most institutional money today?

EXTRA — RESEARCH BEFORE TRADING
  Give me full market context for NVDA before I trade
  Show me NVDA earnings history and how it reacted each time
  Run full technical analysis on GOOGL
  What is the latest news on NVDA?
  Show me the complete options flow signal for IREN

DAILY WORKFLOW (run in order every market day)
  1. What is today's market status?
  2. Show me my active alerts
  3. Show me my active bets with targets and stop losses
  4. Should I sell anything in my portfolio today?
  5. Scan my watchlist and find the top picks today
  6. Give me a full recommendation for [top pick] with $2000 budget
  7. Start monitoring my positions
----------------------------------------------------------
EOF

echo ""
echo "=== SECTION 2: USE CASE 1 — PORTFOLIO ==="

python3 << 'PYEOF'
from app.broker.webull_connector import WebullConnector
from app.utils.current_user import get_current_user_id
from app.broker.sell_signals import get_portfolio_pnl_summary
user_id = get_current_user_id()
pos     = WebullConnector(user_id).get_positions()
pnl     = get_portfolio_pnl_summary(pos, None)
print(f'  Value:    ${pnl["total_value"]:,.2f}')
print(f'  P&L:      ${pnl["total_pnl"]:,.2f} ({pnl["total_pnl_pct"]:+.2f}%)')
print(f'  Win rate: {pnl["win_rate"]}% ({pnl["winners"]}W/{pnl["losers"]}L)')
print(f'  Best: {pnl["best_performer"]} | Worst: {pnl["worst_performer"]}')
print('✅ Portfolio P&L OK')
PYEOF

sleep 5
echo ""
echo "=== SECTION 3: USE CASE 2 — WATCHLIST ==="

python3 << 'PYEOF'
from app.broker.watchlist_sync import get_db_watchlist
from app.utils.current_user import get_current_user_id
tickers = get_db_watchlist(get_current_user_id())
print(f'  Count: {len(tickers)} tickers')
print(f'  Sample: {tickers[:8]}')
print('✅ Watchlist OK')
PYEOF

sleep 5
echo ""
echo "=== SECTION 4: USE CASE 3 — ACTIVE BETS ==="

python3 << 'PYEOF'
from app.broker.webull_connector import WebullConnector
from app.utils.current_user import get_current_user_id
from app.broker.active_bets import get_active_bets
user_id = get_current_user_id()
pos     = WebullConnector(user_id).get_positions()
bets    = get_active_bets(pos, user_id=user_id)
stop_hit   = [b for b in bets if b['status'] == 'STOP_HIT']
target_hit = [b for b in bets if b['status'] == 'TARGET_HIT']
near_stop  = [b for b in bets if b['status'] == 'NEAR_STOP']
near_tgt   = [b for b in bets if b['status'] == 'NEAR_TARGET']
on_track   = [b for b in bets if b['status'] == 'ON_TRACK']
print(f'  Stop hit: {len(stop_hit)} | Near stop: {len(near_stop)} | On track: {len(on_track)} | Near target: {len(near_tgt)} | Target hit: {len(target_hit)}')
if stop_hit:
    print('  STOP HIT:')
    for b in stop_hit:
        print(f'    {b["symbol"]} {b["pnl_pct"]:.1f}% | invested ${b["investment"]:,.0f}')
if target_hit:
    print('  TARGET HIT:')
    for b in target_hit:
        print(f'    {b["symbol"]} +{b["pnl_pct"]:.1f}% | gain ${b["pnl_amount"]:,.0f}')
print('✅ Active Bets OK')
PYEOF

sleep 5
echo ""
echo "=== SECTION 5: USE CASE 4 — SCANNER ==="

python3 << 'PYEOF'
import time
from app.scanner.quick_scan import quick_scan, get_last_trading_date
from app.scanner.universe import get_scan_universe
tickers = get_scan_universe()
t0      = time.time()
picks   = quick_scan(tickers, top_n=5)
print(f'  Scanned {len(tickers)} tickers in {round(time.time()-t0,1)}s — top {len(picks)} picks:')
for p in picks:
    print(f'    {p["ticker"]:6} {p["direction"]:8} {p["change_pct"]:+.2f}%')
print('✅ Scanner OK')
PYEOF

sleep 15
echo ""
echo "=== SECTION 5b: STRATEGY ENGINE ==="

python3 << 'PYEOF'
import time
from app.options_flow.unusual_whales import get_signal_package
from app.options_flow.signals import score_signal_package
from app.strategy.engine import build_recommendation
from app.market_data.polygon_client import get_bars
from app.technical_analysis.engine import get_technical_profile
from datetime import datetime, timedelta
ticker    = 'NVDA'
from_date = (datetime.now()-timedelta(days=300)).strftime('%Y-%m-%d')
bars      = get_bars(ticker, 1, 'day', from_date, datetime.now().strftime('%Y-%m-%d'))
ta        = get_technical_profile(ticker, bars)
signal    = score_signal_package(get_signal_package(ticker))
t0        = time.time()
rec       = build_recommendation(ticker, ta, signal, budget=2000)
best      = rec.get('best', {})
print(f'  Strategy: {best.get("strategy")} | Expiry: {best.get("expiry")} | R/R: {best.get("risk_reward")} | {round(time.time()-t0,1)}s')
print('✅ Strategy Engine OK')
PYEOF

sleep 10
echo ""
echo "=== SECTION 6: USE CASE 5 — POSITION MONITOR ==="

python3 << 'PYEOF'
from app.monitor.position_monitor import get_monitor, get_active_alerts
from app.utils.current_user import get_current_user_id
user_id = get_current_user_id()
monitor = get_monitor(user_id)
fired   = monitor._check_positions(check_manual=True)
alerts  = get_active_alerts(user_id, limit=10)
high    = [a for a in alerts if a['urgency'] == 'HIGH']
medium  = [a for a in alerts if a['urgency'] == 'MEDIUM']
print(f'  Alerts fired: {fired} | Active — HIGH: {len(high)} | MEDIUM: {len(medium)}')
for a in alerts[:3]:
    print(f'  [{a["urgency"]}] {a["symbol"]} — {a["message"][:65]}')
print('✅ Monitor OK')
PYEOF

sleep 15
echo ""
echo "=== SECTION 7: USE CASE 6 — SELL SIGNALS ==="

python3 << 'PYEOF'
from app.broker.webull_connector import WebullConnector
from app.utils.current_user import get_current_user_id
from app.broker.sell_signals import evaluate_sell_signals
user_id = get_current_user_id()
pos     = WebullConnector(user_id).get_positions()
signals = evaluate_sell_signals(pos)
sell    = [s for s in signals if s['action'] == 'SELL']
watch   = [s for s in signals if s['action'] == 'WATCH']
hold    = [s for s in signals if s['action'] == 'HOLD']
print(f'  SELL: {len(sell)} | WATCH: {len(watch)} | HOLD: {len(hold)}')
for s in (sell + watch)[:4]:
    rule = s['signals'][0] if s['signals'] else ''
    print(f'  [{s["urgency"]}] {s["symbol"]} {s["pnl_pct"]:+.1f}% — {rule}')
print('✅ Sell Signals OK')
PYEOF

echo ""
echo "--- sell_recommendations table ---"
docker exec trading_postgres psql -U trading -d trading_platform -c \
"SELECT symbol, pnl_pct, llm_action, LEFT(llm_summary,50) AS summary, recommended_at::date AS date FROM sell_recommendations ORDER BY recommended_at DESC LIMIT 5;"

echo ""
echo "=== SECTION 8: USE CASE 7 — ALERTS + TRACKED ==="

docker exec trading_postgres psql -U trading -d trading_platform -c \
"SELECT symbol, alert_type, urgency, LEFT(message,60) AS message, triggered_at::date AS date FROM position_alerts WHERE dismissed=FALSE ORDER BY CASE urgency WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, triggered_at DESC LIMIT 8;"

docker exec trading_postgres psql -U trading -d trading_platform -c \
"SELECT symbol, source, target_pct, stop_pct, check_interval_min FROM tracked_positions WHERE is_active=TRUE LIMIT 10;"

echo ""
echo "=== SECTION 9: USE CASE 8 — NEW STOCK RESEARCH ==="

python3 << 'PYEOF'
import time
from app.rag.context_builder import build_ticker_context
ticker = 'BE'
t0     = time.time()
ctx    = build_ticker_context(ticker)
print(f'  {ticker} context in {round(time.time()-t0,1)}s')
print(f'  Trend: {ctx["price"].get("trend")} | 30d: {ctx["price"].get("ret_30d")}% | 90d: {ctx["price"].get("ret_90d")}%')
print(f'  Sector: {ctx["sector"].get("sector_etf")} — {ctx["sector"].get("sector_vs_market")}')
earn = ctx['earnings']
if earn.get('upcoming'):
    print(f'  Earnings: {earn["upcoming"]["date"]} ({earn["upcoming"]["days_away"]} days)')
print('✅ RAG Context OK')
PYEOF

echo ""
echo "=== SECTION 10: EXTRA — MARKET INTELLIGENCE ==="

python3 << 'PYEOF'
from app.options_flow.unusual_whales import get_market_tide
tide = get_market_tide()
if isinstance(tide, list): tide = tide[0] if tide else {}
call = tide.get('call_premium') or tide.get('total_call_premium', 'N/A')
put  = tide.get('put_premium')  or tide.get('total_put_premium', 'N/A')
print(f'  Market tide — Calls: {call} | Puts: {put}')
print('✅ Market Tide OK')
PYEOF

sleep 5

python3 << 'PYEOF'
import time
from app.llm.service import _call_ollama
t0 = time.time()
r  = _call_ollama('NVDA up 2% with heavy call sweeps. Bull or bear? One word.', 'Expert trader. One word.', max_tokens=5)
print(f'  LLM: "{r.strip()}" in {round(time.time()-t0,1)}s')
print('✅ LLM OK')
PYEOF

echo ""
echo "=== SECTION 11: DB STATUS ==="

docker exec trading_postgres psql -U trading -d trading_platform -c \
"SELECT 'position_alerts (active)' AS t, COUNT(*) FROM position_alerts WHERE dismissed=FALSE
 UNION ALL SELECT 'sell_recommendations',        COUNT(*) FROM sell_recommendations
 UNION ALL SELECT 'tracked_positions (active)',  COUNT(*) FROM tracked_positions WHERE is_active=TRUE
 UNION ALL SELECT 'muted_symbols',               COUNT(*) FROM muted_symbols
 UNION ALL SELECT 'portfolio_cache',             COUNT(*) FROM portfolio_cache
 UNION ALL SELECT 'user_watchlist',              COUNT(*) FROM user_watchlist
 ORDER BY t;"

echo ""
echo "=== SECTION 12: BLUEPRINT STATUS ==="

cat << 'EOF'
PHASE 1 (W1-W6)  COMPLETE: DB, Webull, MCP, Market Data, Sell Signals, LLM
PHASE 2 (W7-W13) COMPLETE: Options Flow, TA, Strategy, Scanner, Watchlist, RAG, Monitor
EXTRAS COMPLETE: Active Bets, Watchlist Sync, sell_recommendations, tracked_positions, muted_symbols, portfolio_cache

REMAINING:
  W14  Notifications (SMS/Slack)       <- NEXT
  W15  Prediction Tracker
  W16  Learning Engine
  W17  Backtesting
  W18-20 Self-learning loop
  W21  Trade Execution
  W22-24 Dashboard

DEFERRED (needs Polygon Starter $29/mo):
  TA cache in DB — search for: REMOVE THIS LINE when on Polygon Starter plan
EOF

echo ""
echo "=== SECTION 13: MAINTENANCE ==="

cat << 'EOF'
Restart Ollama (if CPU-only):
  pkill -f "ollama serve" && ollama serve &

Force watchlist sync:
  python3 -c "from app.broker.watchlist_sync import force_sync; from app.utils.current_user import get_current_user_id; print(force_sync(get_current_user_id()))"

Clear all alerts (testing):
  docker exec trading_postgres psql -U trading -d trading_platform -c "UPDATE position_alerts SET dismissed=TRUE WHERE dismissed=FALSE;"

Clear RAG cache:
  python3 -c "from app.rag.context_builder import clear_cache; clear_cache(); print('done')"

Git log:
  git log --oneline -10
EOF

echo ""
echo "=== SECTION 14: KNOWN ISSUES ==="

cat << 'EOF'
1. Ollama idle — model not in /api/ps: fine, loads on first call
2. Webull 429: do not call get_positions() twice rapidly — reuse result
3. Market tide AttributeError: check isinstance(tide, list) before .get()
4. Polygon 429: free tier 5 req/min — runbook adds sleeps between sections
   Real fix: Polygon Starter $29/mo = 100 req/min
5. UW flow = 0 on weekends: expected, no live flow outside market hours
6. Reuters RSS blocked: using CNBC + MarketWatch + Fed RSS instead
EOF

echo ""
echo "=== RUNBOOK COMPLETE ==="

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 15 — MCP TOOL REGISTRY (48 tools — no overlaps)
# Reference before adding any new tool
# ═══════════════════════════════════════════════════════════════════════════════

cat << 'EOF'

╔══════════════════════════════════════════════════════════════╗
║              MCP TOOL REGISTRY — 48 TOOLS                   ║
║         Check this before adding any new tool               ║
╚══════════════════════════════════════════════════════════════╝

── PING ────────────────────────────────────────────────────────
  ping                      Health check — DB + server alive

── BROKER / PORTFOLIO ──────────────────────────────────────────
  get_positions             Raw Webull live positions (pre/post market prices)
  get_balances              Account balance + buying power
  get_orders                Today's orders
  get_portfolio_pnl         Financial snapshot: total value, P&L, win rate
  get_active_bets           Trading view: targets, stops, status, action needed

── MARKET DATA ─────────────────────────────────────────────────
  get_market_status         NYSE open/closed, last trading day, next open
  get_quote                 Single ticker previous close (Polygon)
  get_quotes                Bulk previous close (Polygon)
  get_price_history         Historical OHLCV bars
  get_ticker_info           Fundamentals: name, market cap, exchange

── TECHNICAL ANALYSIS ──────────────────────────────────────────
  analyze_ticker            RSI, MACD, BB, ATR, S/R, signal, strength score

── OPTIONS FLOW (Unusual Whales) ───────────────────────────────
  get_market_overview       Market tide, sector flow, economic calendar
  get_options_flow          Institutional sweeps
  get_dark_pool             Block trades
  get_gex                   Gamma exposure, gamma walls
  get_ticker_signal         Combined flow signal: direction + confidence
  get_earnings_calendar     Today earnings pre/post market
  get_news                  UW market headlines (fast, no RAG)
  get_congress_trades       Congressional trading activity

── STRATEGY ────────────────────────────────────────────────────
  get_strategy_recommendation  Full options trade recommendation

── WATCHLIST + SCANNER ─────────────────────────────────────────
  get_watchlist             DB watchlist instant (126 tickers)
  force_sync_watchlist      Immediate Webull sync
  add_to_watchlist          Add ticker
  remove_from_watchlist     Remove ticker
  get_scan_universe         Watchlist + positions + filters (scanner input)
  scan_watchlist            Two-tier convergence scanner + top picks

── SELL SIGNALS ────────────────────────────────────────────────
  get_sell_signals          Exit analysis: rule-based + LLM batch

── RAG / RESEARCH ──────────────────────────────────────────────
  get_market_context        Full RAG: price + earnings + macro + news + sector

── POSITION MONITOR ────────────────────────────────────────────
  start_monitor             Start background polling (15/30 min two-tier)
  stop_monitor              Stop polling
  get_monitor_status        Running status, last check, pending alerts
  get_active_alerts         Unread alerts sorted by urgency
  dismiss_alert             Mark one alert read
  dismiss_all_alerts        Mark all alerts read
  mute_alerts               Mute global or per-symbol, with optional expiry
  unmute_alerts             Re-enable alerts
  get_mute_status           What is currently muted

── NOTIFICATIONS (Discord) ─────────────────────────────────────
  configure_discord         Save webhook + send test
  test_notification         Send test alert to verify setup
  get_notification_config   Show current settings

── PREDICTION TRACKER ──────────────────────────────────────────
  confirm_execution         User confirms they bought a recommendation
  log_outcome               Position closed — record result + update win rate
  mark_sell_acted           Confirm acted on a sell signal
  get_trade_history         Full history of executed trades

── LEARNING ENGINE ─────────────────────────────────────────────
  get_learning_report       FULL: compliance + performance + weights + actions
                            USE THIS — not get_accuracy_report (deprecated)
  get_strategy_weights      Current confidence weight adjustments per strategy
  get_sell_signal_compliance  Quick compliance check (subset of learning report)

── RULES ───────────────────────────────────────────────────────
  DO NOT add duplicate tools — check this list first
  get_accuracy_report is DEPRECATED — use get_learning_report
  get_news vs get_market_context: news=fast headlines, context=full RAG
  get_portfolio_pnl vs get_active_bets: financial vs trading action view
  get_quote vs get_quotes: single vs bulk

EOF

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 16 — TODAY'S NEW COMPONENTS (W16-Step6)
# ═══════════════════════════════════════════════════════════════════════════════

echo ""
echo "=== NEW: POSITION DISAPPEARED DETECTION ==="
# Test monitor catches disappeared positions
python3 << 'PYEOF'
from app.monitor.position_monitor import get_active_alerts
from app.utils.current_user import get_current_user_id
alerts  = get_active_alerts(get_current_user_id())
closed  = [a for a in alerts if a['alert_type'] == 'POSITION_CLOSED']
print(f'POSITION_CLOSED alerts: {len(closed)}')
for a in closed:
    print(f'  {a["symbol"]}: {a["message"][:80]}')
if not closed:
    print('  None — no positions disappeared yet')
PYEOF

echo ""
echo "=== NEW: VIX CONTEXT ==="
python3 << 'PYEOF'
from app.rag.context_builder import _build_vix_context
vix = _build_vix_context()
print(f'VIX: {vix.get("current")} | Zone: {vix.get("zone")} | Trend: {vix.get("trend")}')
print(f'Implication: {vix.get("implication")}')
if vix.get("warning"):
    print(f'Warning: {vix.get("warning")}')
print('✅ VIX context OK')
PYEOF

echo ""
echo "=== NEW: VOLUME CONFIRMATION ==="
python3 << 'PYEOF'
from app.rag.context_builder import _build_price_context
price = _build_price_context('NVDA')
print(f'Relative volume: {price.get("relative_volume")}x 20d avg')
print(f'Signal: {price.get("volume_signal")} | Confirmed: {price.get("volume_confirmed")}')
print(f'Note: {price.get("volume_note")}')
print('✅ Volume confirmation OK')
PYEOF

echo ""
echo "=== NEW: ENTRY TRIGGER + S/R ==="
python3 << 'PYEOF'
from app.rag.context_builder import _build_price_context
price = _build_price_context('GOOGL')
print(f'Price: ${price.get("current_price")}')
print(f'Supports: {price.get("key_supports")}')
print(f'Resistances: {price.get("key_resistances")}')
print(f'Entry trigger: {price.get("entry_trigger")}')
print(f'Entry note: {price.get("entry_note")}')
print('✅ Entry trigger OK')
PYEOF

echo ""
echo "=== NEW: IV RANK ==="
python3 << 'PYEOF'
from app.rag.context_builder import _build_vix_context, _build_iv_context
vix = _build_vix_context()
iv  = _build_iv_context('NVDA', sector_etf='XLK',
      vix=vix.get('current', 17.0) if not vix.get('error') else 17.0)
if iv.get('error'):
    print(f'IV Error: {iv["error"]}')
else:
    print(f'ATM IV: {round(iv.get("atm_iv",0)*100,1)}%')
    print(f'IV Rank: {iv.get("iv_rank")}/100 | Zone: {iv.get("iv_zone")}')
    print(f'Buy options: {iv.get("buy_options")} | {iv.get("strategy_note")}')
    print(f'Days of history: {iv.get("days_of_data")}')
    print('✅ IV rank OK')
PYEOF

echo ""
echo "=== NEW: FULL RAG CONTEXT (all sections) ==="
python3 << 'PYEOF'
import time
from app.rag.context_builder import build_ticker_context, clear_cache
clear_cache()
t0  = time.time()
ctx = build_ticker_context('NVDA')
elapsed = round(time.time()-t0, 1)
prompt  = ctx.get('formatted_prompt', '')
sections = ['[PRICE', '[EARNINGS', '[SECTOR', '[VIX', '[IV RANK', '[GLOBAL NEWS']
print(f'Context built in {elapsed}s')
for s in sections:
    found = s in prompt
    print(f'  {"✅" if found else "❌"} {s}')
print('✅ Full RAG context OK')
PYEOF

echo ""
echo "=== NEW: IV HISTORY TABLE ==="
docker exec trading_postgres psql -U trading -d trading_platform -c \
"SELECT ticker, recorded_at, atm_iv, avg_iv, vix_at_time
 FROM iv_history ORDER BY recorded_at DESC LIMIT 5;"

echo ""
echo "=== NEW: CONFIRM EXECUTION (idempotent) ==="
python3 << 'PYEOF'
from app.learning.prediction_tracker import confirm_execution
from app.utils.current_user import get_current_user_id
user_id = get_current_user_id()
r = confirm_execution(user_id, 'GOOGL', 8.90, 5)
print(f'Confirmed: {r["confirmed"]}')
if r.get("confirmed"):
    print(f'Message: {r["message"]}')
PYEOF

docker exec trading_postgres psql -U trading -d trading_platform -c \
"SELECT symbol, entry_price, qty, source, check_interval_min, is_active
 FROM tracked_positions WHERE symbol='GOOGL';
 SELECT symbol, actual_entry, contracts, user_executed, rec_date
 FROM strategy_recommendations WHERE symbol='GOOGL';"

echo ""
echo "=== NEW: MCP SERVER ALIVE ==="
ps aux | grep "mcp_server" | grep -v grep && echo "✅ MCP server running" || echo "⚠️  MCP server not running as process (managed by Claude Desktop)"