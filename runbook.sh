# Trading Platform — Command Runbook
# Run these in order to validate the full platform before each session.
# Project: ~/Documents/Claude/Projects/trading-platform

# ─────────────────────────────────────────────────────────────────────────────
# 0. ENVIRONMENT SETUP
# ─────────────────────────────────────────────────────────────────────────────

cd ~/Documents/Claude/Projects/trading-platform
source venv/bin/activate

# ─────────────────────────────────────────────────────────────────────────────
# 1. INFRASTRUCTURE HEALTH
# ─────────────────────────────────────────────────────────────────────────────

# 1a. Docker (Postgres)
docker ps | grep trading_postgres && echo "✅ Postgres running" || echo "❌ Postgres down — run: docker-compose up -d"

# 1b. Ollama (local LLM)
curl -s http://localhost:11434/api/tags | python3 -c "
import json, sys
d = json.load(sys.stdin)
models = [m['name'] for m in d.get('models', [])]
print('✅ Ollama running | Models:', models)
" 2>/dev/null || echo "❌ Ollama not running — run: ollama serve &"

# 1c. Ollama GPU check
curl -s http://localhost:11434/api/ps | python3 -c "
import json, sys
d = json.load(sys.stdin)
for m in d.get('models', []):
    gb = round(m.get('size_vram', 0) / 1e9, 1)
    gpu = '✅ Metal GPU' if m.get('size_vram', 0) > 0 else '❌ CPU only'
    print('{} | {} | {:.1f} GB VRAM'.format(m['name'], gpu, gb))
" 2>/dev/null || echo "No model loaded yet"

# 1d. DB connection + tables
python3 -c "
from app.db.session import get_session
from sqlalchemy import text
with get_session() as s:
    tables = s.execute(text(\"SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename\")).fetchall()
    print('✅ DB connected | Tables:', [t[0] for t in tables])
"

# ─────────────────────────────────────────────────────────────────────────────
# 2. WEBULL CONNECTOR (W2)
# ─────────────────────────────────────────────────────────────────────────────

# 2a. Positions
python3 -c "
from app.broker.webull_connector import WebullConnector
from app.utils.current_user import get_current_user_id
wb  = WebullConnector(get_current_user_id())
pos = wb.get_positions()
print('✅ Positions: {} holdings'.format(len(pos)))
for p in pos[:3]:
    rate = float(p.get('unrealized_profit_loss_rate', 0)) * 100
    print('  {} | qty={} | pnl={:+.1f}%'.format(p['symbol'], p['qty'], rate))
"

# 2b. Balances
python3 -c "
from app.broker.webull_connector import WebullConnector
from app.utils.current_user import get_current_user_id
wb  = WebullConnector(get_current_user_id())
try:
    bal = wb.get_balances()
    print('✅ Balances:', {k: v for k, v in list(bal.items())[:4]})
except Exception as e:
    print('⚠️  Balances:', e)
"

# ─────────────────────────────────────────────────────────────────────────────
# 3. WEBULL WATCHLIST (W11)
# ─────────────────────────────────────────────────────────────────────────────

python3 -c "
from app.broker.webull_watchlist_api import get_watchlist_tickers, load_token, is_expiring_soon
from app.utils.current_user import get_current_user_id
user_id    = get_current_user_id()
token_data = load_token(user_id)
if token_data:
    expiring = is_expiring_soon(token_data, hours=48)
    print('Token expiring soon:', expiring)
tickers = get_watchlist_tickers(user_id)
print('✅ Watchlist: {} tickers'.format(len(tickers)))
print('  First 10:', tickers[:10])
"

# ─────────────────────────────────────────────────────────────────────────────
# 4. MARKET DATA (W4)
# ─────────────────────────────────────────────────────────────────────────────

# 4a. Trading calendar
python3 -c "
from app.scanner.quick_scan import get_last_trading_date, _get_last_trading_session, us_market_holidays
from datetime import datetime
print('Today:', datetime.now().date())
print('Last trading day:', get_last_trading_date())
print('Market status:', _get_last_trading_session())
print('2026 holidays:', sorted(str(h) for h in us_market_holidays(2026)))
"

# 4b. Polygon grouped daily (one call, all US stocks)
python3 -c "
import requests, time
from app.utils.config import settings
from app.scanner.quick_scan import get_last_trading_date
date = get_last_trading_date()
t0   = time.time()
r    = requests.get(
    'https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/' + date,
    params={'apiKey': settings.polygon_api_key}, timeout=15)
elapsed = round(time.time()-t0, 2)
if r.status_code == 200:
    data = r.json().get('results', [])
    test = {d['T']: d['c'] for d in data if d.get('T') in ('NVDA','AAPL','AMD','SPY')}
    print('✅ Polygon grouped {} | {} tickers | {}s'.format(date, len(data), elapsed))
    for sym, price in test.items():
        print('  {} \${}'.format(sym, price))
else:
    print('❌ Polygon:', r.status_code, r.text[:100])
"

# ─────────────────────────────────────────────────────────────────────────────
# 5. SCANNER (W10)
# ─────────────────────────────────────────────────────────────────────────────

# 5a. Scan universe (watchlist + positions)
python3 -c "
from app.scanner.universe import get_scan_universe
tickers = get_scan_universe()
print('✅ Scan universe: {} tickers'.format(len(tickers)))
print('  First 15:', tickers[:15])
"

# 5b. Quick scan Tier 1 (prices only, no UW flow on weekends)
python3 -c "
import time
from app.scanner.quick_scan import quick_scan, get_last_trading_date
from app.scanner.universe import get_scan_universe
tickers = get_scan_universe()
print('Scanning {} tickers for {}...'.format(len(tickers), get_last_trading_date()))
t0    = time.time()
picks = quick_scan(tickers, top_n=5)
elapsed = round(time.time()-t0, 1)
print('✅ Scan complete in {}s | {} converging picks'.format(elapsed, len(picks)))
for p in picks:
    print('  {:6} {:+.2f}% | {} | {}'.format(
        p['ticker'], p['change_pct'], p['direction'], ', '.join(p['signals'])))
"

# ─────────────────────────────────────────────────────────────────────────────
# 6. OPTIONS FLOW (W7)
# ─────────────────────────────────────────────────────────────────────────────

python3 -c "
from app.options_flow.unusual_whales import get_flow_alerts, get_market_tide, get_dark_pool_ticker
# Flow alerts
alerts = get_flow_alerts(ticker='NVDA', limit=3)
print('✅ UW Flow alerts (NVDA):', len(alerts))
for a in alerts[:2]:
    print('  {} {} \${} vol={}'.format(
        a.get('ticker'), a.get('sentiment'), a.get('strike'), a.get('volume')))
# Market tide
tide = get_market_tide()
print('✅ Market tide:', tide.get('call_premium'), 'calls vs', tide.get('put_premium'), 'puts')
# Dark pool
dp = get_dark_pool_ticker('NVDA', limit=2)
print('✅ Dark pool (NVDA):', len(dp), 'prints')
"

# ─────────────────────────────────────────────────────────────────────────────
# 7. TECHNICAL ANALYSIS (W8)
# ─────────────────────────────────────────────────────────────────────────────

python3 -c "
from datetime import datetime, timedelta
from app.market_data.polygon_client import get_bars
from app.technical_analysis.engine import get_technical_profile
ticker    = 'NVDA'
from_date = (datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d')
to_date   = datetime.now().strftime('%Y-%m-%d')
bars = get_bars(ticker, 1, 'day', from_date, to_date)
ta   = get_technical_profile(ticker, bars)
print('✅ TA for', ticker)
print('  Signal:', ta.get('signal'))
print('  RSI:', ta.get('rsi_14'))
print('  Trend:', ta.get('trend'))
print('  Summary:', ta.get('summary', '')[:80])
"

# ─────────────────────────────────────────────────────────────────────────────
# 8. LLM SERVICE (W6)
# ─────────────────────────────────────────────────────────────────────────────

python3 -c "
import time
from app.llm.service import _call_ollama
t0 = time.time()
r  = _call_ollama(
    prompt='NVDA is up 2% today with heavy call flow. Bull or bear? One word.',
    system='Expert trader. One word only.',
    max_tokens=5
)
elapsed = round(time.time()-t0, 1)
print('✅ LLM response: \"{}\" | {}s'.format(r.strip(), elapsed))
"

# ─────────────────────────────────────────────────────────────────────────────
# 9. PORTFOLIO P&L + SELL SIGNALS (W5)
# ─────────────────────────────────────────────────────────────────────────────

# 9a. Portfolio P&L (instant — no LLM)
python3 -c "
from app.broker.webull_connector import WebullConnector
from app.utils.current_user import get_current_user_id
from app.broker.sell_signals import get_portfolio_pnl_summary
user_id = get_current_user_id()
pos = WebullConnector(user_id).get_positions()
pnl = get_portfolio_pnl_summary(pos, None)
print('✅ Portfolio P&L')
print('  Value: \${:,.2f} | PnL: \${:,.2f} ({:+.2f}%)'.format(
    pnl['total_value'], pnl['total_pnl'], pnl['total_pnl_pct']))
print('  Win rate: {}% ({} winners / {} losers)'.format(
    pnl['win_rate'], pnl['winners'], pnl['losers']))
print('  Best: {} | Worst: {}'.format(pnl['best_performer'], pnl['worst_performer']))
"

# 9b. Sell signals — rule-based only (no LLM, instant)
python3 -c "
from app.broker.webull_connector import WebullConnector
from app.utils.current_user import get_current_user_id
from app.broker.sell_signals import evaluate_sell_signals
user_id = get_current_user_id()
pos     = WebullConnector(user_id).get_positions()
signals = evaluate_sell_signals(pos)
sell    = [s for s in signals if s['action'] == 'SELL']
watch   = [s for s in signals if s['action'] == 'WATCH']
print('✅ Sell signals (rule-based)')
print('  SELL: {} | WATCH: {} | HOLD: {}'.format(
    len(sell), len(watch), len(signals)-len(sell)-len(watch)))
for s in sell[:3]:
    print('  {} {:+.1f}% — {}'.format(s['symbol'], s['pnl_pct'], s['signals'][0]))
"

# 9c. Full sell signals with LLM (takes ~30-80s)
python3 -c "
import time
from app.broker.webull_connector import WebullConnector
from app.utils.current_user import get_current_user_id
from app.broker.sell_signals import evaluate_sell_signals_with_llm, format_sell_report, get_portfolio_pnl_summary
user_id = get_current_user_id()
pos     = WebullConnector(user_id).get_positions()
pnl     = get_portfolio_pnl_summary(pos, None)
t0      = time.time()
signals = evaluate_sell_signals_with_llm(pos, user_id=user_id)
print(format_sell_report(signals, pnl))
print('Time: {}s'.format(round(time.time()-t0, 1)))
"

# 9d. Check sell_recommendations table
docker exec trading_postgres psql -U trading -d trading_platform -c "
SELECT symbol, pnl_pct, llm_action, llm_exit_pct,
       LEFT(llm_summary, 50) AS summary,
       recommended_at::date AS date
FROM sell_recommendations
ORDER BY recommended_at DESC
LIMIT 10;"

# ─────────────────────────────────────────────────────────────────────────────
# 10. MCP SERVER (W3)
# ─────────────────────────────────────────────────────────────────────────────

# 10a. List all MCP tools
python3 -c "
import subprocess, json
result = subprocess.run(
    ['python3', '-c', '''
from app.mcp_server.server import mcp
import asyncio
async def main():
    tools = await mcp.list_tools()
    print(\"Tools ({}): {}\".format(len(tools), [t.name for t in tools]))
asyncio.run(main())
'''],
    capture_output=True, text=True, cwd='.'
)
print(result.stdout or result.stderr)
"

# 10b. MCP server import check
python3 -c "
from app.mcp_server.server import mcp
print('✅ MCP server imports OK')
"

# ─────────────────────────────────────────────────────────────────────────────
# 11. STRATEGY ENGINE (W9)
# ─────────────────────────────────────────────────────────────────────────────

python3 -c "
from app.options_flow.signals import score_signal_package
from app.options_flow.unusual_whales import get_flow_alerts, get_dark_pool_ticker, get_market_tide
ticker = 'NVDA'
pkg = {
    'ticker':      ticker,
    'flow_alerts': get_flow_alerts(ticker=ticker, limit=10),
    'dark_pool':   get_dark_pool_ticker(ticker, limit=5),
    'market_tide': get_market_tide(),
}
signal = score_signal_package(pkg)
print('✅ Signal package for', ticker)
print('  Direction:', signal.get('direction'))
print('  Confidence:', signal.get('confidence'))
print('  Flow score:', signal.get('flow_score'))
"

# ─────────────────────────────────────────────────────────────────────────────
# 12. QUICK VALIDATION — ALL SYSTEMS (run first every session)
# ─────────────────────────────────────────────────────────────────────────────

python3 -c "
print('=== TRADING PLATFORM HEALTH CHECK ===')
errors = []

# DB
try:
    from app.db.session import get_session
    from sqlalchemy import text
    with get_session() as s:
        s.execute(text('SELECT 1'))
    print('✅ Postgres')
except Exception as e:
    print('❌ Postgres:', e); errors.append('postgres')

# Ollama
try:
    import requests
    r = requests.get('http://localhost:11434/api/tags', timeout=3)
    models = [m['name'] for m in r.json().get('models', [])]
    print('✅ Ollama:', models)
except Exception as e:
    print('❌ Ollama:', e); errors.append('ollama')

# Webull
try:
    from app.broker.webull_connector import WebullConnector
    from app.utils.current_user import get_current_user_id
    pos = WebullConnector(get_current_user_id()).get_positions()
    print('✅ Webull:', len(pos), 'positions')
except Exception as e:
    print('❌ Webull:', e); errors.append('webull')

# Watchlist
try:
    from app.broker.webull_watchlist_api import get_watchlist_tickers
    from app.utils.current_user import get_current_user_id
    tickers = get_watchlist_tickers(get_current_user_id())
    print('✅ Watchlist:', len(tickers), 'tickers')
except Exception as e:
    print('❌ Watchlist:', e); errors.append('watchlist')

# Polygon
try:
    import requests
    from app.utils.config import settings
    from app.scanner.quick_scan import get_last_trading_date
    r = requests.get(
        'https://api.polygon.io/v2/aggs/ticker/NVDA/prev',
        params={'apiKey': settings.polygon_api_key}, timeout=5)
    price = r.json().get('results', [{}])[0].get('c', 0)
    print('✅ Polygon: NVDA prev close \${}'.format(price))
except Exception as e:
    print('❌ Polygon:', e); errors.append('polygon')

# UW
try:
    from app.options_flow.unusual_whales import get_flow_alerts
    alerts = get_flow_alerts(ticker='NVDA', limit=1)
    print('✅ UW Options Flow:', 'OK' if isinstance(alerts, list) else 'empty')
except Exception as e:
    print('❌ UW:', e); errors.append('uw')

# MCP
try:
    from app.mcp_server.server import mcp
    print('✅ MCP Server: imports OK')
except Exception as e:
    print('❌ MCP:', e); errors.append('mcp')

print()
if errors:
    print('❌ Issues found:', errors)
else:
    print('✅ ALL SYSTEMS OPERATIONAL')
print('=====================================')
"