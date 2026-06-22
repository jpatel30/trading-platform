#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# TRADING PLATFORM — HEALTH CHECK WITH AUTO-START
# Checks all systems, starts anything not running, pings all external APIs
# ═══════════════════════════════════════════════════════════════════════════════

cd ~/Documents/Claude/Projects/trading-platform
source venv/bin/activate

echo "╔══════════════════════════════════════════════════╗"
echo "║      TRADING PLATFORM — SYSTEM HEALTH CHECK     ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. DOCKER — auto-start if not running
# ─────────────────────────────────────────────────────────────────────────────
echo "── Infrastructure ──────────────────────────────────"

if ! docker info > /dev/null 2>&1; then
    echo "⚠️  Docker not running — starting..."
    open -a Docker
    echo "   Waiting 15s for Docker to start..."
    sleep 15
fi

if docker info > /dev/null 2>&1; then
    echo "✅ Docker running"
else
    echo "❌ Docker failed to start — open Docker Desktop manually"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2. POSTGRES — auto-start if container not running
# ─────────────────────────────────────────────────────────────────────────────
if ! docker ps | grep trading_postgres > /dev/null 2>&1; then
    echo "⚠️  Postgres not running — starting..."
    docker-compose up -d
    sleep 5
fi

if docker ps | grep trading_postgres > /dev/null 2>&1; then
    python3 << 'PYEOF'
try:
    from app.db.session import get_session
    from sqlalchemy import text
    with get_session() as s:
        count = s.execute(text("SELECT COUNT(*) FROM users")).scalar()
    print(f'✅ Postgres — connected | {count} user(s)')
except Exception as e:
    print(f'❌ Postgres — connected but error: {e}')
PYEOF
else
    echo "❌ Postgres failed to start"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 3. OLLAMA — auto-start if not running
# ─────────────────────────────────────────────────────────────────────────────
if ! curl -s --max-time 2 http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "⚠️  Ollama not running — starting..."
    ollama serve > /tmp/ollama.log 2>&1 &
    sleep 6
fi

python3 << 'PYEOF'
try:
    import requests
    r      = requests.get('http://localhost:11434/api/tags', timeout=4)
    models = [m['name'] for m in r.json().get('models', [])]
    ps     = requests.get('http://localhost:11434/api/ps', timeout=4).json()
    loaded = ps.get('models', [])
    if loaded:
        vram = loaded[0].get('size_vram', 0)
        gpu  = 'Metal GPU' if vram > 0 else 'CPU — restart Ollama natively'
        print(f'✅ Ollama — {models} | {gpu} | {round(vram/1e9,1)}GB VRAM')
    else:
        print(f'✅ Ollama — {models} | idle (loads on first call)')
except Exception as e:
    print(f'❌ Ollama — {e}')
PYEOF

echo ""
echo "── External APIs ───────────────────────────────────"

# ─────────────────────────────────────────────────────────────────────────────
# 4. WEBULL
# ─────────────────────────────────────────────────────────────────────────────
python3 << 'PYEOF'
try:
    from app.broker.webull_connector import WebullConnector
    from app.utils.current_user import get_current_user_id
    pos = WebullConnector(get_current_user_id()).get_positions()
    total_value = sum(float(p.get('market_value', 0)) for p in pos)
    print(f'✅ Webull — {len(pos)} positions | portfolio ${total_value:,.0f}')
except Exception as e:
    print(f'❌ Webull — {e}')
PYEOF

sleep 3

# ─────────────────────────────────────────────────────────────────────────────
# 5. POLYGON
# ─────────────────────────────────────────────────────────────────────────────
python3 << 'PYEOF'
try:
    import requests, time
    from app.utils.config import settings
    t0 = time.time()
    r  = requests.get('https://api.polygon.io/v2/aggs/ticker/NVDA/prev',
        params={'apiKey': settings.polygon_api_key}, timeout=6)
    elapsed = round(time.time()-t0, 2)
    if r.status_code == 200:
        price = r.json().get('results', [{}])[0].get('c', 0)
        print(f'✅ Polygon — NVDA ${price} | {elapsed}s | status {r.status_code}')
    elif r.status_code == 429:
        print(f'⚠️  Polygon — rate limited (429) — free tier 5 req/min')
    else:
        print(f'❌ Polygon — status {r.status_code}')
except Exception as e:
    print(f'❌ Polygon — {e}')
PYEOF

sleep 3

# ─────────────────────────────────────────────────────────────────────────────
# 6. YAHOO FINANCE
# ─────────────────────────────────────────────────────────────────────────────
python3 << 'PYEOF'
try:
    import yfinance as yf, time
    t0   = time.time()
    tick = yf.Ticker('NVDA')
    hist = tick.history(period='1d')
    elapsed = round(time.time()-t0, 2)
    if not hist.empty:
        price = round(hist['Close'].iloc[-1], 2)
        print(f'✅ Yahoo Finance — NVDA ${price} | {elapsed}s')
    else:
        print(f'⚠️  Yahoo Finance — empty response | {elapsed}s')
except Exception as e:
    print(f'❌ Yahoo Finance — {e}')
PYEOF

sleep 3

# ─────────────────────────────────────────────────────────────────────────────
# 7. UNUSUAL WHALES
# ─────────────────────────────────────────────────────────────────────────────
python3 << 'PYEOF'
try:
    import time
    from app.options_flow.unusual_whales import get_flow_alerts, get_market_tide, get_economic_calendar
    t0 = time.time()

    # Flow alerts
    alerts = get_flow_alerts(ticker='NVDA', limit=1)
    elapsed = round(time.time()-t0, 2)
    print(f'✅ UW — flow alerts OK | {elapsed}s')

    # Market tide
    tide = get_market_tide()
    if isinstance(tide, list): tide = tide[0] if tide else {}
    call = tide.get('call_premium') or tide.get('total_call_premium', 'N/A')
    print(f'✅ UW — market tide OK | call premium: {call}')

    # Economic calendar
    events = get_economic_calendar()
    print(f'✅ UW — economic calendar OK | {len(events)} upcoming events')

except Exception as e:
    print(f'❌ UW (Unusual Whales) — {e}')
PYEOF

sleep 3

# ─────────────────────────────────────────────────────────────────────────────
# 8. NEWS APIs (RSS FEEDS)
# ─────────────────────────────────────────────────────────────────────────────
python3 << 'PYEOF'
import requests, time

# Reuters DNS blocked — skip it, use CNBC/MarketWatch/Fed instead
feeds_to_check = {
    'Federal Reserve': 'https://www.federalreserve.gov/feeds/press_all.xml',
    'CNBC Markets':    'https://www.cnbc.com/id/100003114/device/rss/rss.html',
    'MarketWatch':     'https://feeds.marketwatch.com/marketwatch/topstories/',
}
print('⚠️  Reuters — DNS blocked in this network (expected — not used in platform)')

for name, url in feeds_to_check.items():
    try:
        t0 = time.time()
        r  = requests.get(url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = round(time.time()-t0, 2)
        if r.status_code == 200:
            print(f'✅ {name} — OK | {len(r.text):,} chars | {elapsed}s')
        else:
            print(f'⚠️  {name} — status {r.status_code}')
    except Exception as e:
        err = str(e)[:60]
        print(f'❌ {name} — {err}')
PYEOF

sleep 3

# ─────────────────────────────────────────────────────────────────────────────
# 9. POLYGON NEWS
# ─────────────────────────────────────────────────────────────────────────────
python3 << 'PYEOF'
try:
    import requests, time
    from app.utils.config import settings
    t0 = time.time()
    r  = requests.get('https://api.polygon.io/v2/reference/news',
        params={'apiKey': settings.polygon_api_key, 'limit': 3}, timeout=6)
    elapsed = round(time.time()-t0, 2)
    if r.status_code == 200:
        count = len(r.json().get('results', []))
        print(f'✅ Polygon News — {count} articles | {elapsed}s')
    elif r.status_code == 429:
        print(f'⚠️  Polygon News — rate limited (429)')
    else:
        print(f'❌ Polygon News — status {r.status_code}')
except Exception as e:
    print(f'❌ Polygon News — {e}')
PYEOF

echo ""
echo "── Internal Components ─────────────────────────────"

# ─────────────────────────────────────────────────────────────────────────────
# 10. INTERNAL COMPONENT IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
python3 << 'PYEOF'
components = {
    'MCP Server':       'from app.mcp_server.server import mcp',
    'RAG Pipeline':     'from app.rag.context_builder import build_ticker_context',
    'Position Monitor': 'from app.monitor.position_monitor import get_monitor',
    'Active Bets':      'from app.broker.active_bets import get_active_bets',
    'Sell Signals':     'from app.broker.sell_signals import evaluate_sell_signals',
    'Strategy Engine':  'from app.strategy.engine import build_recommendation',
    'Watchlist Sync':   'from app.broker.watchlist_sync import get_db_watchlist',
    'Scanner':          'from app.scanner.quick_scan import quick_scan',
    'TA Engine':        'from app.technical_analysis.engine import get_technical_profile',
}

errors = []
for name, imp in components.items():
    try:
        exec(imp)
        print(f'✅ {name}')
    except Exception as e:
        print(f'❌ {name} — {e}')
        errors.append(name)

print()
if errors:
    print(f'❌ {len(errors)} component(s) failed: {errors}')
else:
    print('✅ All components import OK')
PYEOF

echo ""
echo "── DB Tables ───────────────────────────────────────"

python3 << 'PYEOF'
try:
    from app.db.session import get_session
    from sqlalchemy import text
    with get_session() as s:
        tables = s.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )).fetchall()
        names = [t[0] for t in tables]
        expected = ['broker_connections','monitor_config','muted_symbols',
                    'portfolio_cache','position_alerts','sell_recommendations',
                    'tracked_positions','user_api_keys','user_profiles',
                    'user_watchlist','users']
        missing = [t for t in expected if t not in names]
        print(f'✅ DB tables: {len(names)} total')
        if missing:
            print(f'⚠️  Missing tables: {missing}')
        else:
            print('✅ All expected tables present')
except Exception as e:
    print(f'❌ DB table check failed: {e}')
PYEOF

echo ""
echo "── Watchlist & Cache ───────────────────────────────"

python3 << 'PYEOF'
try:
    from app.broker.watchlist_sync import get_db_watchlist
    from app.monitor.position_monitor import get_cached_portfolio
    from app.utils.current_user import get_current_user_id
    user_id = get_current_user_id()

    tickers = get_db_watchlist(user_id)
    print(f'✅ Watchlist DB — {len(tickers)} tickers')

    cache = get_cached_portfolio(user_id)
    if cache:
        print(f'✅ Portfolio cache — {len(cache["positions"])} positions | age: {cache["age_minutes"]}min | stale: {cache["is_stale"]}')
    else:
        print('ℹ️  Portfolio cache — empty (normal on first run, populates after first monitor check)')
except Exception as e:
    print(f'❌ Watchlist/cache check failed: {e}')
PYEOF

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║                 HEALTH CHECK DONE               ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "If any ❌ above — fix before running the full runbook"
echo "Typical fixes:"
echo "  Docker not starting:  open Docker Desktop manually"
echo "  Ollama CPU only:      pkill -f ollama && ollama serve &"
echo "  Postgres down:        docker-compose up -d"
echo "  Webull 429:           wait 60s and retry"
echo "  Polygon 429:          wait 60s and retry (free tier = 5 req/min)"