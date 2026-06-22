#!/bin/bash
# TRADING PLATFORM — RUNBOOK v3
# Usage: bash runbook.sh

cd ~/Documents/Claude/Projects/trading-platform
source venv/bin/activate

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

sleep 60
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