#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# TRADING PLATFORM — RUNBOOK
# Daily operations, startup, and troubleshooting
# For MCP tool reference see MCP_TOOLS.md
# ═══════════════════════════════════════════════════════════════════════════════

cd ~/Documents/Claude/Projects/trading-platform
source venv/bin/activate

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 0: QUICK STATUS
# ─────────────────────────────────────────────────────────────────────────────

echo "=== QUICK STATUS ==="
bash health_check.sh

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: START EVERYTHING (Sunday night / Monday morning)
# ─────────────────────────────────────────────────────────────────────────────

# Start Docker (Postgres + ChromaDB)
start_docker() {
    if ! docker info > /dev/null 2>&1; then
        open -a Docker
        echo "Waiting for Docker..."
        while ! docker info > /dev/null 2>&1; do sleep 2; done
    fi
    docker compose up -d
    echo "✅ Docker running"
}

# Start Ollama with GPU
start_ollama() {
    if ! pgrep -x "ollama" > /dev/null; then
        ollama serve &
        sleep 3
    fi
    echo "✅ Ollama running ($(ollama list | grep qwen | awk '{print $1}'))"
}

# Start MCP server (for Claude Desktop)
start_mcp() {
    python3 -m app.mcp_server.server &
    echo "✅ MCP server started (PID: $!)"
    echo $! > /tmp/mcp_server.pid
}

# Start FastAPI (for StockBros dashboard)
start_api() {
    uvicorn app.api.main:app --host 0.0.0.0 --port 8001 --reload &
    echo "✅ FastAPI started on :8001 (PID: $!)"
    echo $! > /tmp/fastapi.pid
}

# Start StockBros dashboard
start_dashboard() {
    cd ~/Documents/Claude/Projects/stockbros
    npm run dev &
    echo "✅ Dashboard started on :3000"
    cd -
}

# Keep Mac awake (critical — must run before trading session)
keep_awake() {
    pkill caffeinate 2>/dev/null
    caffeinate -i &
    echo "✅ caffeinate active (PID: $!)"
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: DAILY TRADING WORKFLOW
# ─────────────────────────────────────────────────────────────────────────────

# Morning prep (run at 7-8 AM ET)
morning_prep() {
    echo "=== MORNING PREP ==="
    python3 << 'PYEOF'
from app.recommendations.smart_engine import run_smart_recommendations
from app.utils.current_user import get_current_user_id
import json, time

t0 = time.time()
user_id = get_current_user_id()
result  = run_smart_recommendations(user_id, budget=2000)

print(f"\nMarket: {result.get('market_view')}")
print(f"VIX: {result.get('vix')} ({result.get('vix_zone')})")
print(f"\nOptions Recommendations ({len(result.get('options',[]))} picks):")
for r in result.get('options', []):
    print(f"  {r['ticker']:6} {r['direction']:8} {r.get('strategy','?')}")
    print(f"         exp={r.get('expiry')} conf={r.get('confidence')}/100")
    print(f"         {r.get('reasoning','')[:100]}")

print(f"\nStocks ({len(result.get('stocks',[]))} picks):")
for r in result.get('stocks', []):
    print(f"  {r.get('ticker','?'):6} target=${r.get('target_price',0):.0f} ({r.get('target_pct',0):+.1f}%)")

print(f"\nDone in {time.time()-t0:.1f}s")
PYEOF
}

# Check sell signals (run at market open and close)
check_sells() {
    echo "=== SELL SIGNALS ==="
    python3 << 'PYEOF'
from app.broker.webull_connector import WebullConnector
from app.broker.sell_signals import evaluate_sell_signals
from app.utils.current_user import get_current_user_id

user_id   = get_current_user_id()
positions = WebullConnector(user_id).get_positions()
signals   = evaluate_sell_signals(positions)

urgent = [s for s in signals if s.get('urgency') == 'CLOSE']
if urgent:
    print(f"⚠️  {len(urgent)} URGENT — CLOSE NOW:")
    for s in urgent:
        print(f"   {s['ticker']}: {s['pnl_pct']:.1f}% → {s['signals'][0]}")
else:
    print("✅ No urgent sell signals")

watch = [s for s in signals if s.get('urgency') == 'WATCH']
if watch:
    print(f"\n⚡ {len(watch)} watching:")
    for s in watch:
        print(f"   {s['ticker']}: {s['pnl_pct']:.1f}%")
PYEOF
}

# Confirm a trade after execution
# Usage: confirm_trade NVDA 2 185.50 "2 puts"
confirm_trade() {
    python3 << PYEOF
from app.learning.prediction_tracker import confirm_execution
from app.utils.current_user import get_current_user_id
result = confirm_execution(get_current_user_id(), "$1", $2, "$3")
print(result)
PYEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3: MAINTENANCE
# ─────────────────────────────────────────────────────────────────────────────

# Clean test data (run before first real trade)
clean_test_data() {
    docker exec -i trading_postgres psql -U trading -d trading_platform << 'SQL'
DELETE FROM position_alerts;
DELETE FROM sell_recommendations;
DELETE FROM strategy_recommendations WHERE symbol != 'GOOGL';
DELETE FROM daily_recommendations;
DELETE FROM tracked_positions;
DELETE FROM learning_log;
DELETE FROM news_impact_log;
DELETE FROM portfolio_cache;
SELECT 'Cleaned' as status, COUNT(*) FROM user_watchlist;
SQL
}

# Backup DB
backup_db() {
    DATE=$(date +%Y%m%d_%H%M%S)
    docker exec trading_postgres pg_dump -U trading trading_platform \
        > ~/Documents/Claude/Projects/backups/trading_${DATE}.sql
    echo "✅ Backup saved: trading_${DATE}.sql"
}

# Check UW rate limit usage
check_uw_usage() {
    python3 << 'PYEOF'
import requests
from app.utils.config import settings
r = requests.get("https://api.unusualwhales.com/api/account/usage",
    headers={"Authorization": f"Bearer {settings.unusual_whales_token}"}, timeout=5)
print(r.json())
PYEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4: MONDAY FIRST TRADE CHECKLIST
# ─────────────────────────────────────────────────────────────────────────────

monday_checklist() {
    echo "╔══════════════════════════════════════════════════╗"
    echo "║        MONDAY FIRST TRADE CHECKLIST             ║"
    echo "╚══════════════════════════════════════════════════╝"

    python3 << 'PYEOF'
checks = []

# 1. Infrastructure
try:
    from app.db.session import get_session
    from sqlalchemy import text
    with get_session() as s: s.execute(text("SELECT 1"))
    checks.append(("DB", True, "Postgres connected"))
except Exception as e:
    checks.append(("DB", False, str(e)))

# 2. UW
try:
    from app.options_flow.unusual_whales import get_stock_state
    s = get_stock_state("SPY")
    checks.append(("UW", bool(s), f"SPY=${s.get('price') if s else 'N/A'}"))
except Exception as e:
    checks.append(("UW", False, str(e)))

# 3. Webull
try:
    from app.broker.webull_connector import WebullConnector
    from app.utils.current_user import get_current_user_id
    wb  = WebullConnector(get_current_user_id())
    pos = wb.get_positions()
    checks.append(("Webull", True, f"{len(pos)} positions"))
except Exception as e:
    checks.append(("Webull", False, str(e)))

# 4. LLM
try:
    import requests
    from app.utils.config import settings
    r = requests.get(f"{settings.ollama_host}/api/tags", timeout=3)
    models = [m["name"] for m in r.json().get("models", [])]
    has_qwen = any("qwen" in m for m in models)
    checks.append(("Ollama", has_qwen, f"{'qwen found' if has_qwen else 'qwen NOT found'}"))
except Exception as e:
    checks.append(("Ollama", False, str(e)))

# 5. Watchlist
try:
    from sqlalchemy import text
    from app.db.session import get_session
    with get_session() as s:
        ct = s.execute(text("SELECT COUNT(*) FROM user_watchlist")).scalar()
    checks.append(("Watchlist", ct >= 100, f"{ct} tickers"))
except Exception as e:
    checks.append(("Watchlist", False, str(e)))

# 6. Discord
try:
    from sqlalchemy import text
    from app.db.session import get_session
    with get_session() as s:
        cfg = s.execute(text("SELECT webhook_url FROM notification_config LIMIT 1")).fetchone()
    checks.append(("Discord", bool(cfg), "webhook configured" if cfg else "NOT configured"))
except Exception as e:
    checks.append(("Discord", False, str(e)))

print()
all_ok = True
for name, ok, note in checks:
    icon = "✅" if ok else "❌"
    print(f"  {icon} {name:12} {note}")
    if not ok:
        all_ok = False

print()
if all_ok:
    print("✅ ALL SYSTEMS GO — Ready for first trade")
    print("\nNext steps:")
    print("  1. Run morning_prep() at 7-8 AM ET")
    print("  2. Review highest conviction rec (≥70/100)")
    print("  3. Check: entry trigger + VIX zone + no near earnings")
    print("  4. Execute in Webull")
    print("  5. confirm_trade TICKER QTY PRICE")
else:
    print("❌ FIX ISSUES ABOVE before trading")
PYEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN MENU
# ─────────────────────────────────────────────────────────────────────────────

case "${1}" in
    start)       keep_awake; start_docker; start_ollama; start_mcp; start_api ;;
    dashboard)   start_dashboard ;;
    morning)     morning_prep ;;
    sells)       check_sells ;;
    checklist)   monday_checklist ;;
    clean)       clean_test_data ;;
    backup)      backup_db ;;
    status)      bash health_check.sh ;;
    *)
        echo "Usage: bash RUNBOOK.sh [command]"
        echo ""
        echo "Commands:"
        echo "  start      Start all services (Docker, Ollama, MCP, API)"
        echo "  dashboard  Start StockBros dashboard"
        echo "  morning    Run morning scan (7-8 AM ET)"
        echo "  sells      Check sell signals"
        echo "  checklist  Monday first trade checklist"
        echo "  clean      Clean test data from DB"
        echo "  backup     Backup database"
        echo "  status     Health check"
        ;;
esac
