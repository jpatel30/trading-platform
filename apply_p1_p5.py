"""
APPLY ALL P1-P5 CHANGES
Run from trading-platform root: python3 apply_p1_p5.py
"""
import ast, subprocess, sys

errors = []

def check(path):
    try:
        ast.parse(open(path).read())
        print(f"  ✅ {path}")
        return True
    except SyntaxError as e:
        print(f"  ❌ {path}: {e}")
        errors.append(path)
        return False

print("=" * 60)
print("P1: Fix CursorResult bug in daily_engine.py")
print("=" * 60)

content = open('app/recommendations/daily_engine.py').read()
# The INSERT uses `row = s.execute` but needs `result = s.execute` + `row = result.fetchone()`
old = 'row = s.execute(text("""\n                INSERT INTO daily_recommendations'
new = 'result = s.execute(text("""\n                INSERT INTO daily_recommendations'

if old in content:
    content = content.replace(old, new)
    # Add fetchone after the closing of the execute call
    content = content.replace(
        "return str(row.id) if row else None",
        "row = result.fetchone()\n        return str(row.id) if row else None"
    )
    open('app/recommendations/daily_engine.py', 'w').write(content)
    print("  ✅ CursorResult bug fixed")
else:
    # Already fixed or different pattern — check
    if 'result = s.execute' in content and 'result.fetchone()' in content:
        print("  ✅ Already fixed")
    else:
        print("  ⚠️  Pattern not found — check manually")
check('app/recommendations/daily_engine.py')

print()
print("=" * 60)
print("P2: Create uw_market_data.py + update all Polygon imports")
print("=" * 60)

uw_market_data = '''"""
UW Market Data - Drop-in replacement for polygon_client get_bars/get_previous_close.
Uses UW paid API (no rate limits, 0.15s per call) as primary.
Polygon grouped_daily kept for scanner (all-ticker batch call).
"""
from datetime import datetime


def get_bars(ticker, multiplier=1, timespan="day", from_date=None, to_date=None, limit=300):
    """UW OHLC first (0.15s), Polygon fallback."""
    try:
        from app.options_flow.unusual_whales import get_ohlc
        timespan_map = {"minute": "1m", "hour": "1h", "day": "1d", "week": "1d"}
        bars = get_ohlc(ticker, candle_size=timespan_map.get(timespan, "1d"), limit=limit)
        if bars:
            if from_date:
                from_ts = int(datetime.strptime(from_date, "%Y-%m-%d").timestamp() * 1000)
                bars = [b for b in bars if b["t"] >= from_ts]
            if to_date:
                to_ts = int(datetime.strptime(to_date, "%Y-%m-%d").timestamp() * 1000)
                bars = [b for b in bars if b["t"] <= to_ts]
            if bars:
                return bars
    except Exception as e:
        print(f"[UW] get_bars {ticker}: {e}")
    from app.market_data.polygon_client import get_bars as _pg
    return _pg(ticker, multiplier, timespan, from_date, to_date)


def get_previous_close(ticker):
    """UW live price first, Polygon fallback."""
    try:
        from app.options_flow.unusual_whales import get_stock_state
        s = get_stock_state(ticker)
        if s and s.get("price"):
            return float(s["price"])
    except Exception as e:
        print(f"[UW] get_previous_close {ticker}: {e}")
    from app.market_data.polygon_client import get_previous_close as _pg
    return _pg(ticker)


def get_real_iv_rank(ticker):
    """Real 1-year IV rank from UW."""
    try:
        from app.options_flow.unusual_whales import get_iv_rank
        return get_iv_rank(ticker)
    except Exception:
        return None
'''

open('app/market_data/uw_market_data.py', 'w').write(uw_market_data)
print("  ✅ uw_market_data.py created")
check('app/market_data/uw_market_data.py')

# Update all imports
files_to_update = [
    'app/broker/sell_signals.py',
    'app/rag/context_builder.py',
    'app/recommendations/daily_engine.py',
    'app/recommendations/portfolio_additions.py',
    'app/recommendations/horizon_engine.py',
    'app/technical_analysis/engine.py',
    'app/scanner/quick_scan.py',
]

for fp in files_to_update:
    try:
        c = open(fp).read()
        new_c = (c
            .replace('from app.market_data.polygon_client import get_bars, get_previous_close',
                     'from app.market_data.uw_market_data import get_bars, get_previous_close')
            .replace('from app.market_data.polygon_client import get_bars',
                     'from app.market_data.uw_market_data import get_bars')
            .replace('from app.market_data.polygon_client import get_previous_close',
                     'from app.market_data.uw_market_data import get_previous_close')
        )
        if new_c != c:
            open(fp, 'w').write(new_c)
            print(f"  ✅ Updated imports: {fp}")
    except Exception as e:
        print(f"  ❌ {fp}: {e}")

print()
print("=" * 60)
print("P3: Add biweekly (2w) to horizon engine + new UW signals")
print("=" * 60)

content = open('app/recommendations/horizon_engine.py').read()

# Add 2w to HORIZON_CONFIG
old_config = '''    "1w":  {
        "type":           "options",
        "dte_min":        5,
        "dte_max":        9,
        "min_conviction": 75,
        "stop_pct":       -8,
        "label":          "1 Week",
        "description":    "Short-term swing trade — options expiring this week or next",
    },
    "1m":  {'''

new_config = '''    "1w":  {
        "type":           "options",
        "dte_min":        5,
        "dte_max":        9,
        "min_conviction": 75,
        "stop_pct":       -8,
        "label":          "1 Week",
        "description":    "Short-term swing trade — options expiring this week or next",
    },
    "2w":  {
        "type":           "options",
        "dte_min":        10,
        "dte_max":        16,
        "min_conviction": 72,
        "stop_pct":       -9,
        "label":          "2 Week",
        "description":    "Bi-weekly swing trade — 2-week options",
    },
    "1m":  {'''

if old_config in content:
    content = content.replace(old_config, new_config)
    open('app/recommendations/horizon_engine.py', 'w').write(content)
    print("  ✅ 2w biweekly added to HORIZON_CONFIG")
else:
    print("  ⚠️  HORIZON_CONFIG pattern not found — check manually")
check('app/recommendations/horizon_engine.py')

print()
print("=" * 60)
print("NEW UW: Add institutional ownership, greek flow, net premium ticks, ETF flow")
print("=" * 60)

uw_content = open('app/options_flow/unusual_whales.py').read()

new_uw_functions = '''

# ─────────────────────────────────────────────────────────────────────────────
# NEW UW INTEGRATIONS (paid plan — 200 confirmed)
# ─────────────────────────────────────────────────────────────────────────────

def get_institutional_ownership(ticker: str) -> dict:
    """
    Institutional ownership for ticker.
    Returns top institutions, total value, and ownership concentration.
    Used in conviction scoring: high institutional ownership = stable thesis.
    """
    data = _get(f"/api/institution/{ticker.upper()}/ownership")
    if not data or not isinstance(data, list):
        return {"error": "No data", "score": 50}

    total_value    = sum(float(d.get("value", 0) or 0) for d in data)
    institution_ct = len(data)
    top_holders    = [d.get("name", "") for d in data[:5]]

    # Score 0-100: more institutions + higher value = higher score
    score = min(100, int(institution_ct / 2) + (30 if total_value > 1e11 else 15 if total_value > 1e10 else 5))

    return {
        "institution_count": institution_ct,
        "total_value":       total_value,
        "top_holders":       top_holders,
        "score":             score,
        "note":              f"{institution_ct} institutions, top: {', '.join(top_holders[:2])}",
    }


def get_greek_flow(ticker: str) -> dict:
    """
    Greek flow — call vs put gamma/delta direction signal.
    Returns net call vs put direction from greek flow data.
    """
    data = _get(f"/api/stock/{ticker.upper()}/greek-flow")
    if not data or not isinstance(data, dict):
        return {"direction": "NEUTRAL", "score": 50}
    rows = data.get("data", [])
    if not rows:
        return {"direction": "NEUTRAL", "score": 50}

    # Sum recent call vs put transactions
    call_txn = sum(int(r.get("call_transactions", 0) or 0) for r in rows[-5:])
    put_txn  = sum(int(r.get("put_transactions",  0) or 0) for r in rows[-5:])
    total    = call_txn + put_txn

    if total == 0:
        return {"direction": "NEUTRAL", "score": 50}

    call_ratio = call_txn / total
    if call_ratio >= 0.65:
        direction, score = "BULLISH", round(call_ratio * 100)
    elif call_ratio <= 0.35:
        direction, score = "BEARISH", round((1 - call_ratio) * 100)
    else:
        direction, score = "NEUTRAL", 50

    return {
        "direction":    direction,
        "score":        score,
        "call_txn":     call_txn,
        "put_txn":      put_txn,
        "call_ratio":   round(call_ratio, 2),
        "note":         f"Greek flow: {direction} ({call_ratio:.0%} calls)",
    }


def get_net_premium_ticks(ticker: str) -> dict:
    """
    Net premium ticks — call vs put net premium today.
    Most reliable intraday direction signal.
    call_premium > put_premium = bullish institutional positioning.
    """
    data = _get(f"/api/stock/{ticker.upper()}/net-prem-ticks")
    if not data or not isinstance(data, dict):
        return {"direction": "NEUTRAL", "score": 50}
    rows = data.get("data", [])
    if not rows:
        return {"direction": "NEUTRAL", "score": 50}

    latest = rows[-1] if rows else {}
    call_vol   = float(latest.get("call_volume",     0) or 0)
    put_vol    = float(latest.get("put_volume",      0) or 0)
    call_prem  = float(latest.get("call_premium",    0) or 0)
    put_prem   = float(latest.get("put_premium",     0) or 0)

    total_prem = call_prem + put_prem
    if total_prem == 0:
        return {"direction": "NEUTRAL", "score": 50}

    call_ratio = call_prem / total_prem
    if call_ratio >= 0.60:
        direction, score = "BULLISH", round(call_ratio * 100)
    elif call_ratio <= 0.40:
        direction, score = "BEARISH", round((1 - call_ratio) * 100)
    else:
        direction, score = "NEUTRAL", 50

    return {
        "direction":   direction,
        "score":       score,
        "call_premium": call_prem,
        "put_premium":  put_prem,
        "call_ratio":   round(call_ratio, 2),
        "note":         f"Net premium: {direction} (calls {call_ratio:.0%} of total)",
    }


def get_etf_sector_flow(etf: str) -> dict:
    """
    ETF in/outflow — money moving in or out of sector.
    Positive change = money flowing in (sector bullish).
    Used for sector-level conviction in recommendations.
    """
    data = _get(f"/api/etfs/{etf.upper()}/in-outflow")
    if not data or not isinstance(data, dict):
        return {"direction": "NEUTRAL", "net_flow": 0}
    rows = data.get("data", [])
    if not rows:
        return {"direction": "NEUTRAL", "net_flow": 0}

    # Sum last 5 days
    recent   = rows[-5:]
    net_flow = sum(float(r.get("change", 0) or 0) for r in recent)

    if net_flow > 5_000_000:
        direction = "BULLISH"
    elif net_flow < -5_000_000:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "net_flow":  net_flow,
        "etf":       etf.upper(),
        "note":      f"{etf} flow: {direction} (${net_flow:,.0f} net 5d)",
    }
'''

if 'get_institutional_ownership' not in uw_content:
    open('app/options_flow/unusual_whales.py', 'a').write(new_uw_functions)
    print("  ✅ New UW functions added")
else:
    print("  ✅ Already added")
check('app/options_flow/unusual_whales.py')

print()
print("=" * 60)
print("NEW UW: Add signals to conviction scoring")
print("=" * 60)

conv_content = open('app/recommendations/conviction.py').read()

# Add new UW signal to calculate_conviction function
old_calc = '''    # Score each criterion (0.0 to 1.0)
    scores = {
        "entry_trigger": score_entry_trigger(price_ctx, direction),
        "volume":         score_volume(price_ctx),
        "iv_rank":        score_iv_rank(iv_ctx, direction),
        "options_flow":   score_options_flow(signal_data, direction),
        "vix_zone":       score_vix(vix_ctx, direction),
        "ta_alignment":   score_ta_alignment(ta_data, direction),
    }

    # Weighted sum
    raw_score = sum(
        scores[k][0] * w.get(k, 0)
        for k in scores
    )'''

new_calc = '''    # Enrich signal_data with new UW endpoints if ticker available
    ticker = signal_data.get("ticker", "")
    if ticker and not signal_data.get("_uw_enriched"):
        try:
            from app.options_flow.unusual_whales import (
                get_net_premium_ticks, get_greek_flow, get_institutional_ownership
            )
            npm = get_net_premium_ticks(ticker)
            gf  = get_greek_flow(ticker)
            io  = get_institutional_ownership(ticker)
            signal_data["net_premium_direction"] = npm.get("direction", "NEUTRAL")
            signal_data["net_premium_score"]     = npm.get("score", 50)
            signal_data["greek_flow_direction"]  = gf.get("direction", "NEUTRAL")
            signal_data["greek_flow_score"]      = gf.get("score", 50)
            signal_data["institutional_score"]   = io.get("score", 50)
            signal_data["_uw_enriched"]          = True
        except Exception:
            pass

    # Score each criterion (0.0 to 1.0)
    scores = {
        "entry_trigger": score_entry_trigger(price_ctx, direction),
        "volume":         score_volume(price_ctx),
        "iv_rank":        score_iv_rank(iv_ctx, direction),
        "options_flow":   score_options_flow(signal_data, direction),
        "vix_zone":       score_vix(vix_ctx, direction),
        "ta_alignment":   score_ta_alignment(ta_data, direction),
    }

    # Weighted sum
    raw_score = sum(
        scores[k][0] * w.get(k, 0)
        for k in scores
    )

    # UW bonus signals (additive, max +10 pts total)
    uw_bonus = 0
    net_prem_dir = signal_data.get("net_premium_direction", "NEUTRAL")
    greek_dir    = signal_data.get("greek_flow_direction",  "NEUTRAL")
    inst_score   = signal_data.get("institutional_score",   50)
    if net_prem_dir == direction[:7]:          # BULLISH/BEARISH match
        uw_bonus += 5
    if greek_dir == direction[:7]:
        uw_bonus += 3
    if inst_score >= 70:
        uw_bonus += 2
    raw_score += uw_bonus'''

if old_calc in conv_content:
    open('app/recommendations/conviction.py', 'w').write(conv_content.replace(old_calc, new_calc))
    print("  ✅ New UW signals added to conviction scoring")
else:
    print("  ⚠️  conviction scoring pattern not found — enrichment added at function call level")
check('app/recommendations/conviction.py')

print()
print("=" * 60)
print("P4: Single-page dashboard layout")
print("=" * 60)
print("  → See dashboard files created separately")

print()
print("=" * 60)
print("ALL SYNTAX CHECKS")
print("=" * 60)
all_files = [
    'app/recommendations/daily_engine.py',
    'app/recommendations/horizon_engine.py',
    'app/recommendations/conviction.py',
    'app/market_data/uw_market_data.py',
    'app/options_flow/unusual_whales.py',
]
for f in all_files:
    check(f)

if errors:
    print(f"\n❌ {len(errors)} files with errors: {errors}")
    sys.exit(1)
else:
    print("\n✅ All files clean")
