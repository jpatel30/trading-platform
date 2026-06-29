"""
END-TO-END TEST: Options + Stock recommendations across all horizons
Target: complete in 30-35 seconds
Run from trading-platform root: python3 test_full_system.py
"""
import time
import sys

t_start = time.time()

def elapsed():
    return round(time.time() - t_start, 1)

print(f"[{elapsed()}s] Starting full system test...\n")

# ── Step 1: Health check ──────────────────────────────────────────────────────
print(f"[{elapsed()}s] STEP 1: Health check")
try:
    from app.options_flow.unusual_whales import get_ohlc, get_stock_state, get_iv_rank
    bars  = get_ohlc('SPY', '1d', limit=5)
    state = get_stock_state('SPY')
    iv    = get_iv_rank('SPY')
    print(f"  UW OHLC: {len(bars)} bars | Price: ${state.get('price')} | IV rank: {iv.get('iv_rank'):.1f}/100")
except Exception as e:
    print(f"  ❌ UW failed: {e}")
    sys.exit(1)

# ── Step 2: Scanner (prices for all tickers) ──────────────────────────────────
print(f"\n[{elapsed()}s] STEP 2: Scanner — 127 tickers")
try:
    from app.scanner.quick_scan import quick_scan
    from app.scanner.universe import get_scan_universe
    from app.utils.current_user import get_current_user_id

    user_id = get_current_user_id()
    tickers = get_scan_universe(user_id=user_id)
    picks   = quick_scan(tickers, user_id=user_id, top_n=10)
    print(f"  ✅ {len(picks)} scanner picks from {len(tickers)} tickers in {elapsed()}s")
    for p in picks[:5]:
        print(f"     {p['ticker']:6} {p['direction']:8} score={p.get('score',0):.1f}")
except Exception as e:
    print(f"  ❌ Scanner failed: {e}")
    picks = []

# ── Step 3: No-watchlist fallback ────────────────────────────────────────────
if not tickers or len(tickers) < 5:
    print(f"\n[{elapsed()}s] No watchlist detected — using SPY + QQQ only")
    tickers = ['SPY', 'QQQ']
    picks   = quick_scan(tickers, user_id=user_id, top_n=2)

# ── Step 4: Options recommendations across all horizons ───────────────────────
print(f"\n[{elapsed()}s] STEP 3: Options recommendations (all horizons)")

from app.recommendations.horizon_engine import get_horizon_recommendation, HORIZON_CONFIG
option_horizons = [h for h, cfg in HORIZON_CONFIG.items() if cfg['type'] in ('options', 'both')]

results = {"options": {}, "stocks": {}}

test_ticker = picks[0]['ticker'] if picks else 'SPY'
print(f"  Using {test_ticker} as test ticker")

for horizon in option_horizons:
    try:
        t0  = time.time()
        rec = get_horizon_recommendation(test_ticker, horizon, budget=2000, user_id=user_id)
        dur = round(time.time()-t0, 1)
        best = rec.get('options_rec', rec).get('best', {}) if rec else {}
        dte  = best.get('dte', '?')
        conf = rec.get('options_rec', rec).get('confidence', '?') if rec else '?'
        print(f"  {horizon:4} → DTE={dte} conf={conf} ({dur}s)")
        results["options"][horizon] = rec
    except Exception as e:
        print(f"  {horizon:4} → ERROR: {e}")

# ── Step 5: Stock recommendations ─────────────────────────────────────────────
print(f"\n[{elapsed()}s] STEP 4: Stock recommendations (3m, 6m, 1yr)")

from app.recommendations.horizon_engine import get_stock_for_horizon
import yfinance as yf

stock_ticker = 'NVDA'
for horizon in ['3m', '6m', '1yr']:
    try:
        t0    = time.time()
        price = yf.Ticker(stock_ticker).fast_info.last_price
        rec   = get_stock_for_horizon(stock_ticker, horizon, 5000, current_price=price)
        dur   = round(time.time()-t0, 1)
        if rec.get('filtered'):
            print(f"  {horizon:4} {stock_ticker} → filtered: {rec.get('reason','')[:50]}")
        else:
            print(f"  {horizon:4} {stock_ticker} → target=${rec.get('target_price'):.0f} "
                  f"({rec.get('target_pct'):+.1f}%) fund={rec.get('fundamental_score')}/100 ({dur}s)")
        results["stocks"][horizon] = rec
    except Exception as e:
        print(f"  {horizon:4} {stock_ticker} → ERROR: {e}")

# ── Step 6: Conviction scoring test ──────────────────────────────────────────
print(f"\n[{elapsed()}s] STEP 5: Conviction scoring with new UW signals")
try:
    from app.recommendations.conviction import calculate_conviction
    from app.rag.context_builder import _build_price_context, _build_vix_context, _build_iv_context

    price_ctx = _build_price_context(test_ticker)
    vix_ctx   = _build_vix_context()
    iv_ctx    = _build_iv_context(test_ticker, vix=vix_ctx.get('current', 17))

    signal_data = {
        "ticker":     test_ticker,
        "flow_score": picks[0].get('flow_score', 50) if picks else 50,
        "dp_score":   picks[0].get('dp_score', 50) if picks else 50,
    }

    direction = picks[0].get('direction', 'BEARISH') if picks else 'BEARISH'

    conv = calculate_conviction(
        price_ctx   = price_ctx,
        vix_ctx     = vix_ctx,
        iv_ctx      = iv_ctx,
        ta_data     = {"signal": "NEUTRAL", "trend": "DOWNTREND", "rsi_14": 47, "macd_signal": "BEARISH"},
        signal_data = signal_data,
        direction   = direction,
        llm_confidence = 65,
    )
    print(f"  {test_ticker} {direction}: conviction={conv['conviction_score']}/100 tier={conv['conviction_tier']}")
    print(f"  Passes threshold (70): {conv['passes_threshold']}")
    if signal_data.get('net_premium_direction'):
        print(f"  Net premium: {signal_data['net_premium_direction']} | "
              f"Greek flow: {signal_data.get('greek_flow_direction','N/A')} | "
              f"Institutional: {signal_data.get('institutional_score','N/A')}/100")
except Exception as e:
    print(f"  ❌ Conviction scoring: {e}")

# ── Step 7: DB store test ─────────────────────────────────────────────────────
print(f"\n[{elapsed()}s] STEP 6: DB store test (P1 fix verification)")
try:
    from app.recommendations.daily_engine import _upsert_recommendation
    rec_id = _upsert_recommendation(user_id, {
        "ticker":           "TEST",
        "horizon":          "1m",
        "direction":        "BEARISH",
        "conviction_score": 75,
        "conviction_tier":  "HIGH",
        "act_now":          True,
        "position_size_guidance": "standard",
        "thesis":           "Test thesis — system check",
        "entry_zone_low":   100.0,
        "entry_zone_high":  102.0,
        "entry_trigger":    "AT_RESISTANCE",
        "target_price":     95.0,
        "target_pct":       -5.0,
        "stop_price":       105.0,
        "stop_pct":         3.0,
        "timeframe":        "28 days",
        "invalidation_conditions": "Close above $105",
        "strategy":         "DEBIT_PUT_SPREAD",
        "legs":             [],
        "key_news":         "NONE",
        "warnings":         [],
        "conviction_breakdown": {},
        "signal_data":      {},
    })
    if rec_id:
        print(f"  ✅ DB store works — id: {rec_id}")
        # Clean up test row
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("DELETE FROM daily_recommendations WHERE ticker='TEST'"))
    else:
        print("  ❌ DB store returned None — P1 fix not applied")
except Exception as e:
    print(f"  ❌ DB store failed: {e}")

# ── Summary ───────────────────────────────────────────────────────────────────
total = elapsed()
print(f"\n{'='*50}")
print(f"TOTAL TIME: {total}s")
print(f"TARGET:     30-35s")
print(f"STATUS:     {'✅ ON TARGET' if total <= 35 else '⚠️  OVER TARGET — check slowest steps'}")
print()
print("Options results:")
for h, r in results['options'].items():
    best = r.get('options_rec', r).get('best', {}) if r else {}
    print(f"  {h:4}: DTE={best.get('dte','?')} strategy={best.get('strategy','?')}")
print()
print("Stock results:")
for h, r in results['stocks'].items():
    if r and not r.get('filtered'):
        print(f"  {h:4}: target=${r.get('target_price',0):.0f} ({r.get('target_pct',0):+.1f}%)")
    elif r:
        print(f"  {h:4}: filtered — {r.get('reason','')[:40]}")
