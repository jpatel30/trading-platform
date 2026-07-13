"""
Smart Multi-Horizon Recommendation Engine.

Architecture (target: 20-25s total):
  Phase 1 (parallel): Enrich top candidates simultaneously
    - IV rank + IV term structure, expiries, earnings, news, TA
    - GEX, insider activity (EDGAR), OI buildup (leading indicator),
      velocity (signal_history)
  Phase 2: ONE LLM call with all context
  Phase 3: Deterministic math (real UW prices, real R/R gate)

Rewritten July 2026 — fixed two bugs found live during debugging:
  1. get_gex() doesn't exist on unusual_whales module (only
     get_gex_by_strike/get_gex_by_expiry) — every enrichment call was
     silently raising ImportError, caught by a broad except, so
     gex_score/gex_negative have been 0/False for every candidate this
     entire session regardless of real GEX conditions.
  2. get_velocity_scores([ticker], pick.get("user_id","") or "") — 
     scanner picks never carry a "user_id" key, so this always resolved
     to an empty string, which matches zero rows in signal_history.
     Velocity shown to the LLM (Vel:+0%) has been wrong for every
     candidate in the live-scan enrichment path all session, even
     though the scanner's own Signal 5 (quick_scan.py) correctly used
     the real user_id. _enrich_ticker now takes user_id explicitly.

Flow/dark-pool scoring now imports from app.signals.flow_scoring —
the shared, audited implementation — instead of a local copy.
"""
import json
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.signals.flow_scoring import compute_flow_score, compute_dp_score


# ─────────────────────────────────────────────────────────────────────────────
# Filters for option-tradeable tickers
# ─────────────────────────────────────────────────────────────────────────────

MIN_PRICE      = 15.0
MAX_PRICE      = 800.0
MIN_CONFIDENCE = 50

EXCLUDED = {"NMAX","VXX","UVXY","SQQQ","TQQQ","SPXU","DIA","IWM"}


def _is_optionable(pick: dict) -> bool:
    price = float(pick.get("price", 0) or 0)
    return (
        price >= MIN_PRICE and price <= MAX_PRICE
        and pick.get("ticker") not in EXCLUDED
        and pick.get("confidence", 0) >= MIN_CONFIDENCE
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: Enrich single ticker (runs in parallel for all candidates)
# ─────────────────────────────────────────────────────────────────────────────

def _enrich_ticker(
    pick: dict,
    earnings_map: dict,
    batch_flow: dict,
    batch_dp: dict,
    user_id: str = "",
) -> dict:
    """Enrich one scanner pick with IV, expiries, news, TA, GEX, insider, OI, velocity."""
    from app.options_flow.unusual_whales import (
        get_iv_rank, get_expiry_breakdown, get_ohlc, get_news_headlines,
    )
    from app.technical_analysis.engine import get_technical_profile

    ticker  = pick["ticker"]
    result  = {**pick}

    try:
        iv = get_iv_rank(ticker)
        result["iv_rank"]    = round(iv.get("iv_rank", 50), 1) if iv else 50
        result["iv_current"] = round(iv.get("iv_current", 0.30) * 100, 1) if iv else 30
    except Exception:
        result["iv_rank"], result["iv_current"] = 50, 30

    try:
        expiries_raw = get_expiry_breakdown(ticker)
        today        = datetime.now()
        expiry_list  = []
        iv_est       = result.get("iv_current", 30)
        for e in (expiries_raw or [])[:8]:
            exp_str = e.get("expires", "") or e.get("expiry", "")
            try:
                dte = (datetime.strptime(exp_str, "%Y-%m-%d") - today).days
                if dte < 1:
                    continue
                expiry_list.append({"expiry": exp_str, "dte": dte, "iv_pct": iv_est})
            except Exception:
                continue
        result["expiries"] = expiry_list[:6]
    except Exception:
        result["expiries"] = []

    earnings_info = earnings_map.get(ticker, {})
    result["earnings_days"] = earnings_info.get("days_away", 999)
    result["expected_move"] = earnings_info.get("expected_move_perc", 0)
    result["earnings_date"] = earnings_info.get("report_date", "")

    try:
        news = get_news_headlines(ticker=ticker, limit=3)
        result["news"] = [n.get("headline", "")[:60] for n in (news or [])[:3]]
    except Exception:
        result["news"] = []

    if not result.get("_locked_price"):
        try:
            from app.options_flow.unusual_whales import get_stock_state
            state = get_stock_state(ticker)
            if state and state.get("price"):
                result["price"]       = float(state["price"])
                result["live_price"]  = True
                result["market_time"] = state.get("market_time", "regular")
        except Exception:
            pass
    else:
        result["price"] = result["_locked_price"]
        result["live_price"] = True

    # GEX — get_gex() does not exist; use get_gex_by_strike and take the
    # highest-ranked strike's exposure as the ticker-level signal.
    try:
        from app.options_flow.unusual_whales import get_gex_by_strike
        gex_rows = get_gex_by_strike(ticker) or []
        gex_row  = gex_rows[0] if gex_rows else {}
        gex_val  = float(gex_row.get("total_gex") or gex_row.get("gex") or 0)
        result["gex_score"]    = gex_val
        result["gex_negative"] = gex_val < 0
        result["gex_signal"]   = "ACCELERATION" if gex_val < 0 else "MEAN_REVERSION"
    except Exception:
        result["gex_score"], result["gex_negative"], result["gex_signal"] = 0, False, "UNKNOWN"

    try:
        from app.signals.edgar_insider import get_insider_signal_for_llm, get_insider_activity
        insider = get_insider_activity(ticker, days=5)
        result["insider_signal"]      = insider.get("signal", "NEUTRAL")
        result["insider_text"]        = get_insider_signal_for_llm(ticker)
        result["insider_csuite_buy"]  = insider.get("csuite_buy", False)
        result["insider_csuite_sell"] = insider.get("csuite_sell", False)
    except Exception:
        result["insider_signal"], result["insider_text"] = "NEUTRAL", "No insider data"

    try:
        from app.signals.oi_flow import get_oi_buildup_signal
        oi = get_oi_buildup_signal(ticker)
        result["oi_score"]    = oi.get("score", 0)
        result["oi_signal"]   = oi.get("signal", "NEUTRAL")
        result["oi_max_days"] = oi.get("max_days_building", 0)
    except Exception:
        result["oi_score"], result["oi_signal"], result["oi_max_days"] = 0, "NEUTRAL", 0

    # Velocity — must use the REAL user_id, not pick.get("user_id",""),
    # which is always "" since scanner picks never carry that key.
    try:
        from app.signals.velocity_tracker import get_velocity_scores
        vel = get_velocity_scores([ticker], user_id)
        if vel.get(ticker):
            v = vel[ticker]
            result["velocity"]     = v.get("velocity", 0)
            result["velocity_dir"] = v.get("direction", "STABLE")
            result["days_tracked"] = v.get("days_data", 0)
    except Exception:
        result["velocity"], result["velocity_dir"] = 0, "STABLE"

    try:
        bars = get_ohlc(ticker, "1d", limit=60)
        if bars:
            ta = get_technical_profile(ticker, bars)
            result["rsi"]   = round(ta.get("rsi_14", 50), 1)
            result["trend"] = ta.get("trend", "SIDEWAYS")
            result["macd"]  = ta.get("macd_signal", "NEUTRAL")
        else:
            result["rsi"], result["trend"], result["macd"] = 50, "SIDEWAYS", "NEUTRAL"
    except Exception:
        result["rsi"], result["trend"], result["macd"] = 50, "SIDEWAYS", "NEUTRAL"

    flow_data = batch_flow.get(ticker, {})
    dp_data   = batch_dp.get(ticker, {})
    if flow_data:
        result["flow_score"] = flow_data.get("flow_score", result.get("flow_score", 0))
        result["dp_score"]   = flow_data.get("dp_score",   result.get("dp_score", 0))
        result["sweeps"]     = flow_data.get("sweeps",     result.get("sweeps", 0))
    if dp_data:
        result["dp_score"] = dp_data.get("dp_score", result.get("dp_score", 0))

    return result


def _build_earnings_map(pre: list, post: list) -> dict:
    today   = datetime.now()
    mapping = {}
    for item in (pre or []) + (post or []):
        symbol = item.get("symbol", "")
        try:
            rd   = datetime.strptime(item.get("report_date", ""), "%Y-%m-%d")
            days = max(0, (rd - today).days)
        except Exception:
            days = 999
        mapping[symbol] = {
            "days_away": days,
            "expected_move_perc": float(item.get("expected_move_perc", 0) or 0) * 100,
            "report_date": item.get("report_date", ""),
            "report_time": item.get("report_time", ""),
        }
    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Build LLM prompt + call
# ─────────────────────────────────────────────────────────────────────────────

def _compress_ticker(t: dict) -> str:
    expiry_str = " | ".join(
        f"{e['expiry']}({e['dte']}d,IV{e['iv_pct']:.0f}%)" for e in t.get("expiries", [])[:5]
    ) or "no expiries available"
    news_str = " // ".join(t.get("news", [])[:2]) or "no news"
    earn = t.get("earnings_days", 999)
    earn_str = (f"EARNINGS {earn}d (move±{t.get('expected_move', 0):.1f}%)"
                if earn < 60 else "no near earnings")

    return (
        f"[{t['ticker']} | {t['direction']} | ${t['price']:.2f} | "
        f"{t.get('change_pct', 0):+.1f}% | "
        f"flow:{t.get('flow_score', 0):.0f} dp:{t.get('dp_score', 0):.0f} "
        f"sweeps:{t.get('sweeps', 0)} | "
        f"RSI:{t.get('rsi', 50):.0f} {t.get('trend','?')} {t.get('macd','?')} | "
        f"IV:{t.get('iv_current', 30):.0f}% rank:{t.get('iv_rank', 50):.0f}/100 | "
        f"{earn_str}]\n"
        f"  Expiries: {expiry_str}\n"
        f"  News: {news_str}"
        f" GEX:{'NEG' if t.get('gex_negative') else 'POS'} Vel:{t.get('velocity',0):+.0f}% Insider:{t.get('insider_signal','N')}"
        f" OI:{t.get('oi_score',0):+.0f}({t.get('oi_signal','NEUTRAL')},{t.get('oi_max_days',0)}d)"
        f"{' ⚠️SIGNALS_CONFLICT' if t.get('conflict') else ''}"
    )


def _build_llm_prompt(enriched: list[dict], vix: dict, global_news: list[dict], budget: float, today_str: str) -> str:
    ticker_blocks = "\n\n".join(_compress_ticker(t) for t in enriched)
    news_block = "\n".join(f"  - [{n.get('source','')}] {n.get('headline','')[:80]}" for n in (global_news or [])[:6])

    return f"""You are managing real money. Today is {today_str}.
Budget per trade: ${budget:.0f}. Goal: maximum probability of profit.

=== MARKET CONTEXT ===
VIX: {vix.get('current', 17)} ({vix.get('zone', 'NORMAL')}) trend: {vix.get('trend', 'STABLE')}
Market news:
{news_block}

=== OPTION CANDIDATES (pick the BEST setups) ===
{ticker_blocks}

=== YOUR TASK ===
Study every candidate above. Consider:
- Which ticker has the STRONGEST signal for options today?
- Which expiry maximizes probability of profit (not just DTE rules)?
- Is IV cheap enough to buy? Or should we sell premium?
- What catalyst will move this stock?
- Where are the real entry/exit levels?

Pick up to 4 best option trades.
STRATEGIES AVAILABLE (pick best for situation):
- NAKED_CALL: buy single call — high conviction bullish, no hedge needed
- NAKED_PUT: buy single put — high conviction bearish
- DEBIT_CALL_SPREAD: buy lower strike call, sell higher — moderate bullish, cheaper
- DEBIT_PUT_SPREAD: buy higher strike put, sell lower — moderate bearish, cheaper
- STRADDLE: buy ATM call + ATM put (same strike) — big move expected, direction unknown
- STRANGLE: buy OTM call + OTM put — cheaper than straddle, needs bigger move
- IRON_CONDOR: sell OTM call spread + sell OTM put spread — range-bound market

STRIKE RULES:
- DEBIT_PUT_SPREAD: buy_strike HIGHER than sell_strike (e.g. buy 190p sell 180p)
- DEBIT_CALL_SPREAD: buy_strike LOWER than sell_strike (e.g. buy 240c sell 250c)
- NAKED calls/puts: set buy_strike only, set sell_strike = 0
- STRADDLE/STRANGLE: buy_strike = call strike, sell_strike = put strike
- Max 5-10% OTM. Choose expiry from list above ONLY.

Respond with valid JSON only:
{{
  "market_view": "one sentence on today's market bias",
  "recommendations": [
    {{
      "ticker": "NVDA", "direction": "BEARISH", "expiry": "2026-07-17", "dte": 19,
      "strategy": "DEBIT_PUT_SPREAD", "buy_strike": 190.0, "sell_strike": 182.5,
      "reasoning": "2 sentences: why this ticker, why this expiry",
      "key_risk": "1 sentence on main risk", "confidence": 72, "catalyst": "what will move it"
    }}
  ],
  "skip": ["NMAX", "CBRS"], "skip_reason": "too illiquid / no catalyst"
}}"""


def _call_smart_llm(prompt: str, ticker: str = "MULTI") -> dict | None:
    from app.utils.config import settings
    import requests as req
    import re

    system = """You are an expert options trader managing real client money.
You analyze signals holistically and make decisive, specific recommendations.
Always pick the expiry where the trade has the best risk/reward.
Respond with valid JSON only — no text before or after."""

    try:
        payload = {
            "model":  settings.ollama_model, "prompt": prompt, "system": system, "stream": False,
            "options": {"num_predict": 10000, "temperature": 0.05, "top_p": 0.9, "num_ctx": 8192},
        }
        r   = req.post(f"{settings.ollama_host}/api/generate", json=payload, timeout=120)
        raw = r.json().get("response", "").strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            if ("recommendations" in data or "morning_status" in data
                    or "new_picks" in data or "picks" in data):
                return data
        print(f"[SmartLLM] Could not parse: {raw[:400]}")
        print(f"[SmartLLM] Full response length: {len(raw)} chars")
        return None
    except Exception as e:
        print(f"[SmartLLM] Error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Deterministic math per LLM decision
# ─────────────────────────────────────────────────────────────────────────────

def _execute_smart_rec(rec: dict, budget: float, user_id: str | None) -> dict | None:
    from app.strategy.engine import _execute_trade_math, normalize_strategy

    ticker    = rec.get("ticker", "")
    expiry    = rec.get("expiry", "")
    direction = rec.get("direction", "NEUTRAL")
    strategy  = normalize_strategy(rec.get("strategy", "DEBIT_PUT_SPREAD"))
    buy_str   = float(rec.get("buy_strike", 0) or 0)
    sell_str  = float(rec.get("sell_strike", 0) or 0)
    dte       = int(rec.get("dte", 21) or 21)

    from app.options_flow.unusual_whales import get_stock_state
    state = get_stock_state(ticker)
    spot  = float(state.get("price", 0)) if state else 0
    if not spot:
        try:
            import yfinance as yf
            spot = yf.Ticker(ticker).fast_info.last_price or 0
        except Exception:
            pass

    if not spot or not buy_str:
        return None

    if "DEBIT_CALL_SPREAD" in strategy and buy_str > sell_str:
        buy_str, sell_str = sell_str, buy_str
        print(f"[SmartMath] Auto-corrected CALL spread strikes: BUY ${buy_str} SELL ${sell_str}")
    elif "DEBIT_PUT_SPREAD" in strategy and buy_str < sell_str:
        buy_str, sell_str = sell_str, buy_str
        print(f"[SmartMath] Auto-corrected PUT spread strikes: BUY ${buy_str} SELL ${sell_str}")
    elif "CREDIT_CALL_SPREAD" in strategy and buy_str < sell_str:
        buy_str, sell_str = sell_str, buy_str
        print(f"[SmartMath] Auto-corrected CREDIT CALL strikes: BUY ${buy_str} SELL ${sell_str}")
    elif "CREDIT_PUT_SPREAD" in strategy and buy_str > sell_str:
        buy_str, sell_str = sell_str, buy_str
        print(f"[SmartMath] Auto-corrected CREDIT PUT strikes: BUY ${buy_str} SELL ${sell_str}")

    try:
        from app.options_flow.unusual_whales import get_option_contracts
        real_contracts = get_option_contracts(ticker, expiry=expiry, limit=200)
        real_strikes = set()
        for c in real_contracts:
            sym = c.get("option_symbol", "")
            for marker in ("C", "P"):
                idx = sym.rfind(marker)
                if idx > 0 and sym[idx+1:].isdigit() and len(sym[idx+1:]) == 8:
                    real_strikes.add(round(int(sym[idx+1:]) / 1000.0, 2))
        if real_strikes:
            orig_buy, orig_sell = buy_str, sell_str
            buy_str  = min(real_strikes, key=lambda s: abs(s - buy_str))
            sell_str = min(real_strikes, key=lambda s: abs(s - sell_str))
            if buy_str == sell_str:
                remaining = sorted(real_strikes - {buy_str})
                if remaining:
                    sell_str = min(remaining, key=lambda s: abs(s - orig_sell))
            if (buy_str, sell_str) != (orig_buy, orig_sell):
                print(f"[SmartMath] Snapped to real strikes: BUY ${orig_buy}→${buy_str} SELL ${orig_sell}→${sell_str}")
    except Exception as e:
        print(f"[SmartMath] Strike validation skipped: {e}")

    if "DEBIT_CALL_SPREAD" in strategy and buy_str > sell_str:
        buy_str, sell_str = sell_str, buy_str
        print(f"[SmartMath] Post-snap correction CALL: BUY ${buy_str} SELL ${sell_str}")
    elif "DEBIT_PUT_SPREAD" in strategy and buy_str < sell_str:
        buy_str, sell_str = sell_str, buy_str
        print(f"[SmartMath] Post-snap correction PUT: BUY ${buy_str} SELL ${sell_str}")

    is_credit = "CREDIT" in strategy or "IRON" in strategy

    if strategy == "NAKED_CALL":
        legs = [{"action": "BUY", "type": "CALL", "strike": buy_str}]
    elif strategy == "NAKED_PUT":
        legs = [{"action": "BUY", "type": "PUT", "strike": buy_str}]
    elif strategy == "STRADDLE":
        legs = [{"action": "BUY", "type": "CALL", "strike": buy_str},
                {"action": "BUY", "type": "PUT",  "strike": buy_str}]
    elif strategy == "STRANGLE":
        legs = [{"action": "BUY", "type": "CALL", "strike": sell_str},
                {"action": "BUY", "type": "PUT",  "strike": buy_str}]
    elif strategy == "IRON_CONDOR":
        width = round((sell_str - buy_str) * 0.5, 1)
        legs = [
            {"action": "SELL", "type": "CALL", "strike": sell_str},
            {"action": "BUY",  "type": "CALL", "strike": sell_str + width},
            {"action": "SELL", "type": "PUT",  "strike": buy_str},
            {"action": "BUY",  "type": "PUT",  "strike": buy_str - width},
        ]
    elif is_credit:
        leg_type = "PUT" if "PUT" in strategy else "CALL"
        legs = [{"action": "SELL", "type": leg_type, "strike": buy_str},
                {"action": "BUY",  "type": leg_type, "strike": sell_str}]
    else:
        leg_type = "PUT" if "PUT" in strategy else "CALL"
        legs = [{"action": "BUY",  "type": leg_type, "strike": buy_str},
                {"action": "SELL", "type": leg_type, "strike": sell_str}]

    decision = {
        "strategy": strategy, "expiry": expiry, "dte": dte, "legs": legs,
        "direction": direction, "confidence": int(rec.get("confidence", 65) or 65),
        "reasoning": rec.get("reasoning", ""), "key_risk": rec.get("key_risk", ""),
        "key_news": rec.get("catalyst", "NONE"), "regime_check": "PASS",
    }

    try:
        max_loss = budget * 0.40
        trade    = _execute_trade_math(decision, ticker, spot, budget, max_loss)

        if "DEBIT" in strategy and "CREDIT" not in strategy:
            ed = trade.get("entry_debit", 0) or 0
            if ed <= 0:
                print(f"[SmartMath] {ticker} rejected: negative entry_debit={ed} for {strategy}")
                return None

        trade["ticker"]      = ticker
        trade["direction"]   = direction
        trade["reasoning"]   = rec.get("reasoning", "")
        trade["catalyst"]    = rec.get("catalyst", "")
        trade["key_risk"]    = rec.get("key_risk", "")
        trade["horizon"]     = f"{dte}d"
        trade["rec_type"]    = "options"
        trade["confidence"]  = int(rec.get("confidence", 65) or 65)
        trade["expiry"]      = expiry
        trade["ticker_disp"] = ticker
        return trade
    except Exception as e:
        print(f"[SmartMath] {ticker} failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def run_smart_recommendations(
    user_id: str, budget: float = 2000.0, top_picks: int = 15,
    pre_scanned: list | None = None,
) -> dict:
    from app.scanner.quick_scan import quick_scan
    from app.scanner.universe import get_scan_universe
    from app.rag.context_builder import _build_vix_context, _build_global_news
    from app.options_flow.unusual_whales import (
        get_earnings_premarket, get_earnings_afterhours,
        get_flow_alerts, get_dark_pool_recent,
    )

    t_total = time.time()
    today   = datetime.now().strftime("%A %B %d, %Y")

    print(f"[SmartEngine] Starting run — budget=${budget:.0f}")
    t0 = time.time()

    vix         = _build_vix_context()
    global_news = _build_global_news()
    earnings_pre  = get_earnings_premarket()  or []
    earnings_post = get_earnings_afterhours() or []
    earnings_map  = _build_earnings_map(earnings_pre, earnings_post)
    all_flow      = get_flow_alerts(limit=500)       or []
    all_dp        = get_dark_pool_recent(limit=200)  or []

    flow_by = {}
    for a in all_flow:
        flow_by.setdefault(a.get("ticker",""), []).append(a)
    dp_by = {}
    for d in all_dp:
        dp_by.setdefault(d.get("ticker",""), []).append(d)

    batch_flow = {t: compute_flow_score(alerts) for t, alerts in flow_by.items()}
    batch_dp   = {t: compute_dp_score(prints)   for t, prints in dp_by.items()}

    print(f"[SmartEngine] Shared data in {time.time()-t0:.1f}s | "
          f"VIX={vix.get('current')} | news={len(global_news)} | "
          f"earnings={len(earnings_pre+earnings_post)}")

    if pre_scanned:
        picks = pre_scanned
        print(f"[SmartEngine] Using {len(picks)} pre-scanned picks")
    else:
        print(f"[SmartEngine] Running scanner...")
        t0      = time.time()
        tickers = get_scan_universe(user_id=user_id)
        picks   = quick_scan(tickers, user_id=user_id, top_n=top_picks)

    candidates = [p for p in picks if _is_optionable(p)]
    print(f"[SmartEngine] Scanner: {len(picks)} picks → {len(candidates)} optionable in {time.time()-t0:.1f}s")

    if not candidates:
        candidates = [
            {"ticker": "SPY", "direction": "BEARISH", "price": 590, "confidence": 60,
             "change_pct": 0, "flow_score": 0, "dp_score": 0, "sweeps": 0, "alert_count": 0, "score": 0.5, "signals": []},
            {"ticker": "QQQ", "direction": "BEARISH", "price": 510, "confidence": 60,
             "change_pct": 0, "flow_score": 0, "dp_score": 0, "sweeps": 0, "alert_count": 0, "score": 0.5, "signals": []},
        ]
        print("[SmartEngine] No candidates — using SPY/QQQ fallback")

    print(f"[SmartEngine] Enriching {len(candidates)} candidates in parallel...")
    t0       = time.time()
    enriched = []

    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
        futures = {
            ex.submit(_enrich_ticker, c, earnings_map, batch_flow, batch_dp, user_id): c["ticker"]
            for c in candidates
        }
        for future in as_completed(futures, timeout=30):
            try:
                enriched.append(future.result())
            except Exception as e:
                print(f"[SmartEngine] Enrich failed: {e}")

    print(f"[SmartEngine] Enriched {len(enriched)} tickers in {time.time()-t0:.1f}s")

    enriched.sort(key=lambda x: (
        abs(x.get("flow_score", 0)) + abs(x.get("dp_score", 0)) +
        abs(x.get("change_pct", 0)) * 2 + (20 if x.get("earnings_days", 999) < 14 else 0)
    ), reverse=True)

    print(f"[SmartEngine] Calling LLM with {len(enriched)} candidates...")
    t0     = time.time()
    prompt = _build_llm_prompt(enriched[:10], vix, global_news, budget, today)
    llm_result = _call_smart_llm(prompt)
    print(f"[SmartEngine] LLM responded in {time.time()-t0:.1f}s")

    if not llm_result:
        return {"error": "LLM call failed", "elapsed": round(time.time()-t_total, 1)}

    print(f"[SmartEngine] Market view: {llm_result.get('market_view','')}")
    print(f"[SmartEngine] Recommendations: {len(llm_result.get('recommendations',[]))}")

    t0    = time.time()
    final = []
    for rec in (llm_result.get("recommendations") or []):
        trade = _execute_smart_rec(rec, budget, user_id)
        if trade:
            final.append(trade)
            print(f"[SmartEngine] ✅ {rec['ticker']} {rec['direction']} {rec['strategy']} exp={rec['expiry']} conf={rec.get('confidence')}")
        else:
            print(f"[SmartEngine] ⚠️  {rec.get('ticker')} math failed")

    print(f"[SmartEngine] Math done in {time.time()-t0:.1f}s")

    stock_recs = []
    try:
        from app.recommendations.horizon_engine import get_stock_for_horizon
        import yfinance as yf
        for t in ["NVDA", "AAPL"]:
            try:
                price = yf.Ticker(t).fast_info.last_price or 0
                if price:
                    rec = get_stock_for_horizon(t, "6m", budget*2, current_price=price)
                    if rec and not rec.get("filtered"):
                        stock_recs.append(rec)
            except Exception:
                pass
    except Exception as e:
        print(f"[SmartEngine] Stock recs failed: {e}")

    market_view = llm_result.get("market_view", "")
    for rec in final:
        try:
            from app.recommendations.daily_engine import _upsert_recommendation
            legs = rec.get("legs", [])
            rr_pct = round((rec.get("risk_reward") or 0) * 100, 1)
            _upsert_recommendation(user_id, {
                "ticker": rec["ticker"], "horizon": rec.get("horizon", "17d"),
                "direction": rec["direction"], "conviction_score": rec.get("confidence", 65),
                "conviction_tier": "HIGH" if rec.get("confidence",0)>=75
                                    else "MODERATE" if rec.get("confidence",0)>=65 else "WATCH",
                "act_now": rec.get("confidence", 0) >= 70,
                "position_size_guidance": "standard", "thesis": rec.get("reasoning", ""),
                "entry_zone_low": abs(rec.get("entry_debit", 0)),
                "entry_zone_high": abs(rec.get("entry_debit", 0)) * 1.05,
                "entry_trigger": "AT_MARKET", "target_price": 0, "target_pct": rr_pct,
                "stop_price": 0, "stop_pct": -40.0,
                "timeframe": f"{rec.get('dte', 17)} days",
                "invalidation_conditions": rec.get("key_risk", ""),
                "strategy": rec.get("strategy", ""), "expiry": rec.get("expiry", ""),
                "dte": rec.get("dte", 17), "legs": legs, "entry_debit": rec.get("entry_debit", 0),
                "webull_limit_price": rec.get("webull_limit_price", 0),
                "total_cost": rec.get("total_cost", 0),
                "max_profit": rec.get("max_profit_per_contract", 0),
                "max_loss": rec.get("max_loss_per_contract", 0),
                "risk_reward": rec.get("risk_reward", 0),
                "webull_instructions": rec.get("webull_instructions", ""),
                "key_news": rec.get("catalyst", "NONE"), "warnings": rec.get("engine_warnings", []),
                "conviction_breakdown": {}, "signal_data": {"market_view": market_view},
            })
            print(f"[SmartEngine] Stored rec: {rec['ticker']}")
        except Exception as e:
            print(f"[SmartEngine] Store failed for {rec.get('ticker','?')}: {e}")

    total_time = round(time.time()-t_total, 1)
    print(f"\n[SmartEngine] COMPLETE in {total_time}s — {len(final)} option recs + {len(stock_recs)} stock recs")

    return {
        "market_view": llm_result.get("market_view", ""), "options": final, "stocks": stock_recs,
        "skipped": llm_result.get("skip", []), "skip_reason": llm_result.get("skip_reason", ""),
        "candidates_scanned": len(candidates), "elapsed": total_time,
        "vix": vix.get("current"), "vix_zone": vix.get("zone"), "date": today,
    }
