"""
PARALLEL TEST: Options thread + Stock thread simultaneously
Target: 20-25s total
"""
import time, concurrent.futures, sys

t_start = time.time()
def ts(): return f"[{time.time()-t_start:.1f}s]"

print(f"{ts()} Starting parallel options + stock scan...\n")

from app.utils.current_user import get_current_user_id
user_id = get_current_user_id()

# ── Pre-fetch shared data (once) ──────────────────────────────────────────────
print(f"{ts()} Pre-fetching shared data...")
from app.scanner.quick_scan import quick_scan
from app.scanner.universe import get_scan_universe
from app.rag.context_builder import _build_vix_context
from app.options_flow.unusual_whales import get_flow_alerts, get_dark_pool_recent

tickers = get_scan_universe(user_id=user_id)

# Pre-fetch VIX once
vix = _build_vix_context()
print(f"{ts()} VIX: {vix.get('zone')} {vix.get('current')}")

# Scanner with batch UW (now 2 calls not 254)
picks = quick_scan(tickers, user_id=user_id, top_n=10)
print(f"{ts()} Scanner: {len(picks)} picks")

# Pick a liquid ticker (prefer NVDA/AAPL/SPY from picks, else default)
LIQUID = {'NVDA','AAPL','SPY','QQQ','AMZN','GOOGL','MSFT','AMD','META','TSLA'}
liquid_picks = [p for p in picks if p['ticker'] in LIQUID]
test_ticker  = liquid_picks[0]['ticker'] if liquid_picks else 'NVDA'
print(f"{ts()} Test ticker: {test_ticker}\n")

results = {}

# ── SMART ENGINE (replaces separate options/stocks threads) ────────────────────
def run_smart():
    from app.recommendations.smart_engine import run_smart_recommendations
    print(f"{ts()} [SMART] Starting — 1 LLM call for all horizons")
    return run_smart_recommendations(user_id, budget=2000, top_picks=15, pre_scanned=picks)

# ── RUN IN PARALLEL ───────────────────────────────────────────────────────────
print(f"{ts()} Launching smart engine...")
result = run_smart()
results["options"] = result.get("options", [])
results["stocks"]  = result.get("stocks", [])
results["market_view"] = result.get("market_view", "")
results["elapsed"] = result.get("elapsed", 0)

# ── SUMMARY ───────────────────────────────────────────────────────────────────
total = round(time.time()-t_start, 1)
print(f"\n{'='*50}")
print(f"Market: {results.get('market_view','')}")
print(f"TOTAL: {total}s | TARGET: 25s | {'✅ ON TARGET' if total<=25 else f'⚠️  {total-25:.0f}s over'}")
print(f"\nOptions ({len(results['options'])} recs):")
for r in results["options"]:
    print(f"  {r.get('ticker','?'):6} {r.get('direction','?'):8} "
          f"exp={r.get('expiry','?')} strategy={r.get('strategy','?')} "
          f"cost=${r.get('entry_debit',r.get('total_cost',0)):.0f} "
          f"conf={r.get('confidence','?')}")
print(f"\nStocks ({len(results['stocks'])} recs):")
for r in results["stocks"]:
    print(f"  {r.get('ticker',r.get('symbol','?')):6} target=${r.get('target_price',0):.0f} "
          f"({r.get('target_pct',0):+.1f}%) fund={r.get('fundamental_score','?')}/100")
