#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# TRADING PLATFORM — RUNBOOK
# Daily operations, startup, health check, and troubleshooting — one file.
# For MCP tool reference see MCP_TOOLS.md
# ═══════════════════════════════════════════════════════════════════════════════

cd ~/Documents/Claude/Projects/trading-platform
source venv/bin/activate

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 0: HEALTH CHECK
# ─────────────────────────────────────────────────────────────────────────────

health_check() {
    echo "╔══════════════════════════════════════════════════╗"
    echo "║      TRADING PLATFORM — HEALTH CHECK            ║"
    echo "╚══════════════════════════════════════════════════╝"
    echo ""

    python3 << 'PYEOF'
import sys, time, requests
errors, warnings = [], []

print("── Infrastructure ──────────────────────────────────")
try:
    from app.db.session import get_session
    from sqlalchemy import text
    with get_session() as s:
        wl_ct   = s.execute(text("SELECT COUNT(*) FROM user_watchlist")).scalar()
        user_ct = s.execute(text("SELECT COUNT(*) FROM users")).scalar()
    print(f"  ✅ Postgres  — {wl_ct} watchlist tickers, {user_ct} users")
except Exception as e:
    print(f"  ❌ Postgres  — {e}"); errors.append("postgres")

try:
    from app.utils.config import settings
    r = requests.get(f"{settings.ollama_host}/api/tags", timeout=5)
    models = [m["name"] for m in r.json().get("models", [])]
    qwen   = next((m for m in models if "qwen" in m.lower()), None)
    print(f"  {'✅' if qwen else '⚠️ '} Ollama    — {qwen or 'model not found'}")
    if not qwen: warnings.append("ollama_model")
except Exception as e:
    print(f"  ❌ Ollama    — {e}"); errors.append("ollama")

import subprocess
r2 = subprocess.run(["docker","ps","--format","{{.Names}}"], capture_output=True, text=True)
running = [c for c in r2.stdout.strip().split("\n") if c]
pg_ok   = any("postgres" in c or "trading" in c for c in running)
print(f"  {'✅' if pg_ok else '❌'} Docker    — {', '.join(running[:3]) or 'no containers'}")
if not pg_ok: errors.append("docker")

print("\n── Data Sources ────────────────────────────────────")
try:
    from app.options_flow.unusual_whales import get_stock_state, get_iv_rank
    t0 = time.time()
    s  = get_stock_state("SPY")
    iv = get_iv_rank("SPY")
    print(f"  ✅ UW        — SPY ${s['price']:.2f} ({s.get('market_time','?')}) | "
          f"IV rank {iv.get('iv_rank',0):.1f}/100 | {time.time()-t0:.2f}s")
except Exception as e:
    print(f"  ❌ UW        — {e}"); errors.append("uw")

try:
    from app.market_data.polygon_client import get_grouped_daily
    from datetime import datetime, timedelta
    t0   = time.time()
    data = get_grouped_daily((datetime.now()-timedelta(days=1)).strftime("%Y-%m-%d"))
    ct = len(data or [])
    label = '✅' if ct > 100 else '⚠️ '
    note  = 'market closed (weekend/holiday)' if ct == 0 else f'{ct} tickers'
    print(f'  {label} Polygon   — {note} | {time.time()-t0:.2f}s')
except Exception as e:
    print(f"  ❌ Polygon   — {e}"); errors.append("polygon")

try:
    import yfinance as yf
    t0  = time.time()
    vix = yf.Ticker("^VIX").fast_info.last_price or 0
    print(f"  ✅ yfinance  — VIX {vix:.2f} | {time.time()-t0:.2f}s")
except Exception as e:
    print(f"  ⚠️  yfinance  — {e}"); warnings.append("yfinance")

print("\n── Multi-Agent Pipeline (search/news/prediction/bull/bear) ──")
try:
    import subprocess as _sp
    import pytz as _pytz
    from datetime import datetime as _dt

    # These run as subprocesses spawned + supervised by the FastAPI
    # process (app/api/main.py startup_event/shutdown_event), not
    # launchd — checked here by OS process presence, matching that.
    SERVICE_MODULES = [
        "app.services.search_agent_service", "app.services.news_agent_service",
        "app.services.prediction_agent_service", "app.services.bull_agent_service",
        "app.services.bear_agent_service",
    ]
    ps_out = _sp.run(["ps", "ax", "-o", "args="], capture_output=True, text=True).stdout
    for module in SERVICE_MODULES:
        ok = module in ps_out
        print(f"  {'✅' if ok else '❌'} {module:38} — {'running' if ok else 'NOT RUNNING (run: bash runbook.sh start)'}")
        if not ok: errors.append(module)

    # Data freshness — this is what actually catches a service that's
    # "loaded" but silently failing every run (bad creds, API down,
    # etc). Only checked once its own scheduled trigger time has
    # passed today, and only on weekdays these jobs actually fire.
    et, pt = _pytz.timezone("America/New_York"), _pytz.timezone("America/Los_Angeles")
    now_et, now_pt = _dt.now(et), _dt.now(pt)
    is_weekday = now_et.weekday() < 5

    def _due(now, hour, minute):
        return is_weekday and (now.hour, now.minute) >= (hour, minute)

    from sqlalchemy import text as _text
    with get_session() as s:
        search_ct = s.execute(_text(
            "SELECT COUNT(*) FROM search_agent_snapshot WHERE scan_date = CURRENT_DATE")).scalar()
        news_ct = s.execute(_text(
            "SELECT COUNT(*) FROM news_agent_snapshot WHERE snapshot_date = CURRENT_DATE")).scalar()
        cand_ct = s.execute(_text(
            "SELECT COUNT(*) FROM candidate_directions WHERE scan_date = CURRENT_DATE")).scalar()
        debate_ct = s.execute(_text(
            "SELECT COUNT(*) FROM debate_requests WHERE created_at::date = CURRENT_DATE")).scalar()

    # Earliest trigger each day is 6:00AM PT (pre_open); post_close
    # (4:15PM ET) covers the rest of the day once it fires too.
    if _due(now_pt, 6, 0):
        label = '✅' if search_ct > 0 else '❌'
        print(f"  {label} search_agent_snapshot (today) — {search_ct} row(s)")
        if search_ct == 0: errors.append("search_agent_snapshot_stale")
        label = '✅' if news_ct > 0 else '❌'
        print(f"  {label} news_agent_snapshot (today)   — {news_ct} row(s)")
        if news_ct == 0: errors.append("news_agent_snapshot_stale")
    else:
        print("  ⏳ search/news snapshots — not due yet today (first trigger 6:00AM PT)")

    # candidate_directions/debate_requests can legitimately be empty on
    # a slow day (no qualifying candidates) - warning, not an error.
    if _due(now_pt, 6, 20):
        label = '✅' if cand_ct > 0 else '⚠️ '
        print(f"  {label} candidate_directions (today)  — {cand_ct} row(s)")
        if cand_ct == 0: warnings.append("candidate_directions_stale")
    if _due(now_pt, 8, 50):
        label = '✅' if debate_ct > 0 else '⚠️ '
        print(f"  {label} debate_requests (today)       — {debate_ct} row(s)")
        if debate_ct == 0: warnings.append("debate_requests_stale")
except Exception as e:
    print(f"  ❌ Multi-agent pipeline — {e}"); errors.append("multiagent_pipeline")

print("\n── Core Engine ─────────────────────────────────────")
try:
    from app.recommendations.smart_engine import run_smart_recommendations
    print("  ✅ Smart engine    — importable")
except Exception as e:
    print(f"  ❌ Smart engine    — {e}"); errors.append("smart_engine")

try:
    from app.options_flow.unusual_whales import get_flow_alerts, get_dark_pool_recent
    t0   = time.time()
    flow = get_flow_alerts(limit=50)
    dp   = get_dark_pool_recent(limit=50)
    print(f"  ✅ Batch UW flow   — {len(flow)} alerts, {len(dp)} dp | {time.time()-t0:.2f}s")
except Exception as e:
    print(f"  ⚠️  Batch UW flow   — {e}"); warnings.append("uw_flow")

try:
    r3 = requests.get("http://localhost:8001/api/health", timeout=3)
    d  = r3.json()
    print(f"  ✅ FastAPI :8001   — db:{d.get('db')} llm:{d.get('llm')}")
except Exception:
    print("  ⚠️  FastAPI :8001   — not running"); warnings.append("fastapi")

print("\n── Summary ─────────────────────────────────────────")
if not errors and not warnings:
    print("  ✅ ALL SYSTEMS GO")
elif not errors:
    print(f"  ⚠️  {len(warnings)} warnings: {', '.join(warnings)}")
else:
    print(f"  ❌ {len(errors)} errors: {', '.join(errors)}")
    sys.exit(1)
PYEOF
}

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

# Stop FastAPI — a clean SIGTERM (not -9) so its shutdown_event handler
# runs and terminates the 5 agent-service subprocesses it owns, rather
# than orphaning them.
stop_api() {
    if [ -f /tmp/fastapi.pid ] && kill -0 "$(cat /tmp/fastapi.pid)" 2>/dev/null; then
        kill "$(cat /tmp/fastapi.pid)"
        echo "🛑 FastAPI stopped (search/news/prediction/bull/bear-agent services stop with it)"
        rm -f /tmp/fastapi.pid
    else
        echo "⚠️  FastAPI does not appear to be running (no valid /tmp/fastapi.pid)"
    fi
}

# The 5 agent services (search/news/prediction/bull/bear) are no longer
# started here directly. start_api's FastAPI process spawns + supervises
# all 5 itself now (app/api/main.py's startup_event/shutdown_event) —
# they were originally meant to run under launchd instead, but launchd-
# spawned processes can't access this project's path under ~/Documents
# (macOS TCC/Full-Disk-Access blocks that for background/non-interactive
# processes), while this interactively-launched process already can, so
# children it spawns inherit that access with no new permission grant.
# They stay genuinely separate OS processes talking only through
# Postgres, same as the launchd design intended — just supervised by the
# app instead of by launchd. Starting/stopping the API starts/stops all
# 5 together; see logs/<service>_service.log for each one's own output.

# Start StockBros dashboard
start_dashboard() {
    cd ~/Documents/Claude/Projects/stockbros
    npm run dev &
    echo "✅ Dashboard started on :3000"
    cd -
}

# Keep Mac awake (critical — must run before trading session)
# Runs under launchd now (~/Library/LaunchAgents/com.tradingplatform.
# keep-awake.plist) instead of a bare `&` process — RunAtLoad+KeepAlive
# means it survives reboots and restarts itself if killed, closing the
# exact gap that let the Mac idle-sleep through a weekend and silently
# cause the 5 agent services to miss every scheduled fire (no crash, no
# error - the process just wasn't awake at the trigger instant, and
# APScheduler doesn't catch up a missed fire by default). Manually
# managing `caffeinate -i &` here would fight launchd's KeepAlive
# (each would keep resurrecting the other), so this just ensures the
# launchd job is loaded rather than owning the process itself.
keep_awake() {
    local target="gui/$(id -u)/com.tradingplatform.keep-awake"
    if launchctl print "$target" > /dev/null 2>&1; then
        echo "✅ caffeinate already running under launchd"
    else
        launchctl bootstrap "gui/$(id -u)" \
            ~/Library/LaunchAgents/com.tradingplatform.keep-awake.plist
        echo "✅ caffeinate started under launchd"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: DAILY TRADING WORKFLOW
# ─────────────────────────────────────────────────────────────────────────────

# Run the real multi-agent options-prediction pipeline once, on demand:
# Search -> News -> Prediction routing/enqueue -> Bull -> Bear -> Prediction
# finalize (opens real paper-trade positions, counts against the shared
# 20/day DAILY_PICK_CAP - this is the actual production pipeline, same
# code the scheduled services run, just triggered manually with
# trigger="manual" instead of waiting for the next cron fire). This is
# the multi-agent equivalent of morning_prep() below, which still calls
# the older single-LLM-call smart_engine - both remain valid (smart_engine
# also still backs several single-ticker MCP tools), this is just the one
# that matches the current architecture (ARCHITECTURE.md).
run_pipeline() {
    echo "=== MULTI-AGENT PIPELINE (manual run) ==="
    python3 << 'PYEOF'
import time
TRIGGER = "manual"
t0 = time.time()

from app.services.search_agent_service import _run as search_run
from app.services.news_agent_service import _run as news_run
from app.services.prediction_agent_service import _enqueue_routed, _finalize
from app.services.bull_agent_service import _run as bull_run
from app.services.bear_agent_service import _run as bear_run

print(f"[1/6] Search Agent ({TRIGGER})"); search_run(TRIGGER)
print(f"[2/6] News Agent ({TRIGGER})");   news_run(TRIGGER)
print(f"[3/6] Prediction Agent — enqueue_routed ({TRIGGER})"); _enqueue_routed(TRIGGER)
print(f"[4/6] Bull Agent");  bull_run()
print(f"[5/6] Bear Agent");  bear_run()
print(f"[6/6] Prediction Agent — finalize ({TRIGGER})"); _finalize(TRIGGER)

print(f"\nDone in {round(time.time()-t0, 1)}s — see daily_recommendations/"
      f"tracked_positions for what opened.")
PYEOF
}

# Morning prep (run at 7-8 AM ET) — legacy single-LLM-call path
# (smart_engine.py), predates the multi-agent pipeline above. Kept
# because several single-ticker MCP tools (get_strategy_recommendation,
# get_horizon_recommendation) still use this engine for on-demand deep
# dives - see MCP_TOOLS.md. For the actual daily multi-agent picks, use
# run_pipeline above instead.
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
from app.learning.prediction_tracker import get_positions_from_tracked
from app.broker.sell_signals import evaluate_sell_signals
from app.utils.current_user import get_current_user_id

user_id   = get_current_user_id()
positions = get_positions_from_tracked(user_id)
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
DELETE FROM daily_recommendations;
DELETE FROM tracked_positions;
DELETE FROM learning_log;
DELETE FROM news_impact_log;
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

# 3. Tracked positions (no broker linking exists anymore — item 27/31 —
# confirm_execution/tracked_positions is the only source of truth now)
try:
    from app.learning.prediction_tracker import get_positions_from_tracked
    from app.utils.current_user import get_current_user_id
    pos = get_positions_from_tracked(get_current_user_id())
    checks.append(("Positions", True, f"{len(pos)} tracked positions"))
except Exception as e:
    checks.append(("Positions", False, str(e)))

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
        cfg = s.execute(text("SELECT discord_webhook FROM notification_config LIMIT 1")).fetchone()
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
    print("  4. Execute the trade with your broker")
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
    pipeline)    run_pipeline ;;
    morning)     morning_prep ;;
    sells)       check_sells ;;
    checklist)   monday_checklist ;;
    clean)       clean_test_data ;;
    backup)      backup_db ;;
    status)      health_check ;;
    stop)        stop_api ;;
    *)
        echo "Usage: bash runbook.sh [command]"
        echo ""
        echo "Commands:"
        echo "  start      Start all services (Docker, Ollama, MCP, API — API spawns the"
        echo "             Search/News/Prediction/Bull/Bear Agent services itself)"
        echo "  dashboard  Start StockBros dashboard"
        echo "  pipeline   Run the real multi-agent pipeline once (Search/News/Prediction/"
        echo "             Bull/Bear/Finalize) — opens real paper positions, same as the"
        echo "             scheduled services but triggered now instead of on cron"
        echo "  morning    Run morning scan (7-8 AM ET, legacy single-LLM-call engine)"
        echo "  sells      Check sell signals"
        echo "  checklist  Monday first trade checklist"
        echo "  clean      Clean test data from DB"
        echo "  backup     Backup database"
        echo "  status     Health check (was: health_check.sh, now merged in here)"
        echo "  stop       Stop the API (and the 5 agent services it owns)"
        ;;
esac
