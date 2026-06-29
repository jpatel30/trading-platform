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
    print(f"  {'✅' if len(data or [])>100 else '⚠️ '} Polygon   — {len(data or [])} tickers | {time.time()-t0:.2f}s")
except Exception as e:
    print(f"  ❌ Polygon   — {e}"); errors.append("polygon")

try:
    import yfinance as yf
    t0  = time.time()
    vix = yf.Ticker("^VIX").fast_info.last_price or 0
    print(f"  ✅ yfinance  — VIX {vix:.2f} | {time.time()-t0:.2f}s")
except Exception as e:
    print(f"  ⚠️  yfinance  — {e}"); warnings.append("yfinance")

print("\n── Webull ──────────────────────────────────────────")
try:
    from app.broker.webull_connector import WebullConnector
    from app.utils.current_user import get_current_user_id
    t0  = time.time()
    wb  = WebullConnector(get_current_user_id())
    pos = wb.get_positions()
    bal = wb.get_balance()
    acct = (bal.get("account_currency_assets") or [{}])[0]
    net  = float(acct.get("net_liquidation_value") or 0)
    cash = float(bal.get("total_cash_balance") or 0)
    print(f"  ✅ Webull    — {len(pos)} positions | Net liq ${net:,.0f} | Cash ${cash:,.0f} | {time.time()-t0:.2f}s")
except Exception as e:
    print(f"  ❌ Webull    — {e}"); errors.append("webull")

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
