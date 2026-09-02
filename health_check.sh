#!/bin/bash
cd ~/Documents/Claude/Projects/trading-platform
source venv/bin/activate 2>/dev/null

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
