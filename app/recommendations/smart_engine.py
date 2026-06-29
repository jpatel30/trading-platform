"""
Smart Multi-Horizon Recommendation Engine.

Architecture (target: 20-25s total):
  Phase 1 (5s parallel): Enrich top 15 scanner picks simultaneously
    - IV rank + IV term structure per ticker (UW, 0.12s each)
    - Earnings proximity + expected move (already fetched)
    - News headlines (UW, 3 per ticker)
    - TA from UW bars (0.15s each)
    All run in parallel → max(individual times) not sum

  Phase 2 (18s): ONE LLM call with all context
    - Compressed 50-token context per ticker (~750 tokens for 15 tickers)
    - Full market context (VIX, sector flow, global news, economic calendar)
    - ALL available expiries with IV per expiry for each candidate
    - LLM decides: best ticker per timeframe, best expiry, best strategy, best strikes
    - Output: JSON with 4-6 recommendations

  Phase 3 (1s): Deterministic math
    - Validate LLM strikes against real UW contracts
    - Calculate cost, max profit, max loss, R/R
    - Apply position sizing
    - Store in daily_recommendations

Key insight: LLM sees the full picture and picks expiry freely.
No artificial DTE buckets. If Monday has a catalyst, LLM picks this-week expiry.
If market is quiet, LLM picks monthly. Goal = make money, not follow rules.
"""
import json
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


# ─────────────────────────────────────────────────────────────────────────────
# Filters for option-tradeable tickers
# ─────────────────────────────────────────────────────────────────────────────

MIN_PRICE      = 15.0    # below this = no liquid options
MAX_PRICE      = 800.0   # above this = too expensive for $2K budget
MIN_CONFIDENCE = 50      # scanner confidence threshold

EXCLUDED = {"NMAX","VXX","UVXY","SQQQ","TQQQ","SPXU","DIA","IWM"}  # no individual options play


def _is_optionable(pick: dict) -> bool:
    price = float(pick.get("price", 0) or 0)
    return (
        price >= MIN_PRICE
        and price <= MAX_PRICE
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
) -> dict:
    """
    Enrich one scanner pick with IV, expiries, news, TA.
    Returns compressed context dict ready for LLM prompt.
    """
    from app.options_flow.unusual_whales import (
        get_iv_rank, get_expiry_breakdown, get_ohlc, get_news_headlines,
    )
    from app.technical_analysis.engine import get_technical_profile

    ticker  = pick["ticker"]
    result  = {**pick}  # start with scanner fields

    # IV rank
    try:
        iv = get_iv_rank(ticker)
        result["iv_rank"]    = round(iv.get("iv_rank", 50), 1) if iv else 50
        result["iv_current"] = round(iv.get("iv_current", 0.30) * 100, 1) if iv else 30
    except Exception:
        result["iv_rank"], result["iv_current"] = 50, 30

    # Expiry breakdown — dates only, use overall iv_rank for all
    # (removed per-expiry option_contracts call — was 8 extra UW calls per ticker)
    try:
        expiries_raw = get_expiry_breakdown(ticker)
        today        = datetime.now()
        expiry_list  = []
        iv_est       = result.get("iv_current", 30)
        for e in (expiries_raw or [])[:8]:
            exp_str = e.get("expiry", "")
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

    # Earnings proximity
    earnings_info = earnings_map.get(ticker, {})
    result["earnings_days"]   = earnings_info.get("days_away", 999)
    result["expected_move"]   = earnings_info.get("expected_move_perc", 0)
    result["earnings_date"]   = earnings_info.get("report_date", "")

    # Recent news
    try:
        news = get_news_headlines(ticker=ticker, limit=3)
        result["news"] = [n.get("headline", "")[:60] for n in (news or [])[:3]]
    except Exception:
        result["news"] = []

    # Live price from UW (overrides stale scanner price from Polygon grouped_daily)
    try:
        from app.options_flow.unusual_whales import get_stock_state
        state = get_stock_state(ticker)
        if state and state.get("price"):
            result["price"]      = float(state["price"])
            result["live_price"] = True
            result["market_time"] = state.get("market_time", "regular")
    except Exception:
        pass

    # TA from UW bars
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

    # Merge batch flow/dp
    flow_data = batch_flow.get(ticker, {})
    dp_data   = batch_dp.get(ticker, {})
    if flow_data:
        result["flow_score"] = flow_data.get("flow_score", result.get("flow_score", 0))
        result["dp_score"]   = flow_data.get("dp_score",   result.get("dp_score", 0))
        result["sweeps"]     = flow_data.get("sweeps",     result.get("sweeps", 0))

    return result


def _build_earnings_map(pre: list, post: list) -> dict:
    """Build {ticker: earnings_info} from UW earnings data."""
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
            "days_away":          days,
            "expected_move_perc": float(item.get("expected_move_perc", 0) or 0) * 100,
            "report_date":        item.get("report_date", ""),
            "report_time":        item.get("report_time", ""),
        }
    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Build LLM prompt + call
# ─────────────────────────────────────────────────────────────────────────────

def _compress_ticker(t: dict) -> str:
    """Build a compact ~60-token context block for one ticker."""
    expiry_str = " | ".join(
        f"{e['expiry']}({e['dte']}d,IV{e['iv_pct']:.0f}%)"
        for e in t.get("expiries", [])[:5]
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
    )


def _build_llm_prompt(
    enriched: list[dict],
    vix: dict,
    global_news: list[dict],
    budget: float,
    today_str: str,
) -> str:
    """Build the single combined LLM prompt."""

    ticker_blocks = "\n\n".join(_compress_ticker(t) for t in enriched)

    news_block = "\n".join(
        f"  - [{n.get('source','')}] {n.get('headline','')[:80]}"
        for n in (global_news or [])[:6]
    )

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

Pick up to 4 best option trades (can be same or different tickers for different setups).
STRIKE RULES: DEBIT_PUT_SPREAD → buy_strike HIGHER than sell_strike (e.g. buy 190p sell 180p).
DEBIT_CALL_SPREAD → buy_strike LOWER than sell_strike (e.g. buy 240c sell 250c).
Strikes must be realistic for the stock price — within 5-10% OTM maximum.
For each: choose expiry from the list shown above ONLY.

Respond with valid JSON only:
{{
  "market_view": "one sentence on today's market bias",
  "recommendations": [
    {{
      "ticker": "NVDA",
      "direction": "BEARISH",
      "expiry": "2026-07-17",
      "dte": 19,
      "strategy": "DEBIT_PUT_SPREAD",
      "buy_strike": 190.0,
      "sell_strike": 182.5,
      "reasoning": "2 sentences: why this ticker, why this expiry",
      "key_risk": "1 sentence on main risk",
      "confidence": 72,
      "catalyst": "what will move it"
    }}
  ],
  "skip": ["NMAX", "CBRS"],
  "skip_reason": "too illiquid / no catalyst"
}}"""


def _call_smart_llm(prompt: str, ticker: str = "MULTI") -> dict | None:
    """Single LLM call for all recommendations."""
    from app.utils.config import settings
    import requests as req
    import re

    system = """You are an expert options trader managing real client money.
You analyze signals holistically and make decisive, specific recommendations.
Always pick the expiry where the trade has the best risk/reward.
Respond with valid JSON only — no text before or after."""

    try:
        payload = {
            "model":  settings.ollama_model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {
                "num_predict": 1200,
                "temperature": 0.05,
                "top_p":       0.9,
                "num_ctx":     8192,
            }
        }
        r   = req.post(f"{settings.ollama_host}/api/generate", json=payload, timeout=120)
        raw = r.json().get("response", "").strip()

        # Extract JSON
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            if "recommendations" in data:
                return data
        print(f"[SmartLLM] Could not parse: {raw[:200]}")
        return None
    except Exception as e:
        print(f"[SmartLLM] Error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Deterministic math per LLM decision
# ─────────────────────────────────────────────────────────────────────────────

def _execute_smart_rec(rec: dict, budget: float, user_id: str | None) -> dict | None:
    """Run deterministic trade math for one LLM recommendation."""
    from app.strategy.engine import _execute_trade_math, _uw_price_for_strike, _bsm_greeks
    from app.options_flow.unusual_whales import get_option_contracts

    ticker    = rec.get("ticker", "")
    expiry    = rec.get("expiry", "")
    direction = rec.get("direction", "NEUTRAL")
    strategy  = rec.get("strategy", "DEBIT_PUT_SPREAD")
    buy_str   = float(rec.get("buy_strike", 0) or 0)
    sell_str  = float(rec.get("sell_strike", 0) or 0)
    dte       = int(rec.get("dte", 21) or 21)

    # Build a decision dict that _execute_trade_math expects
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

    # Validate and correct strike order — LLM sometimes inverts them
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

    # Determine leg types from strategy
    is_put    = "PUT" in strategy
    leg_type  = "PUT" if is_put else "CALL"
    is_credit = "CREDIT" in strategy

    if is_credit:
        legs = [
            {"action": "SELL", "type": leg_type, "strike": buy_str},
            {"action": "BUY",  "type": leg_type, "strike": sell_str},
        ]
    else:
        legs = [
            {"action": "BUY",  "type": leg_type, "strike": buy_str},
            {"action": "SELL", "type": leg_type, "strike": sell_str},
        ]

    decision = {
        "strategy":     strategy,
        "expiry":       expiry,
        "dte":          dte,
        "legs":         legs,
        "direction":    direction,
        "confidence":   int(rec.get("confidence", 65) or 65),
        "reasoning":    rec.get("reasoning", ""),
        "key_risk":     rec.get("key_risk", ""),
        "key_news":     rec.get("catalyst", "NONE"),
        "regime_check": "PASS",
    }

    try:
        max_loss = budget * 0.40
        trade    = _execute_trade_math(decision, ticker, spot, budget, max_loss)
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
    user_id:    str,
    budget:     float = 2000.0,
    top_picks:  int   = 15,
    pre_scanned: list | None = None,   # pass existing picks to skip re-scan
) -> dict:
    """
    Full smart recommendation run.
    Returns options + stock recommendations with reasoning.
    """
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

    # ── Pre-fetch shared data (batch) ─────────────────────────────────────────
    print(f"[SmartEngine] Pre-fetching shared data...")
    t0 = time.time()

    vix         = _build_vix_context()
    global_news = _build_global_news()
    earnings_pre  = get_earnings_premarket()  or []
    earnings_post = get_earnings_afterhours() or []
    earnings_map  = _build_earnings_map(earnings_pre, earnings_post)
    all_flow      = get_flow_alerts(limit=500)       or []
    all_dp        = get_dark_pool_recent(limit=500)  or []

    flow_by = {}
    for a in all_flow:
        flow_by.setdefault(a.get("ticker",""), []).append(a)
    dp_by = {}
    for d in all_dp:
        dp_by.setdefault(d.get("ticker",""), []).append(d)

    # Build batch flow dict
    batch_flow = {}
    for ticker, alerts in flow_by.items():
        bull = sum(1 for a in alerts if a.get("sentiment") in ("BULLISH","CALL"))
        bear = sum(1 for a in alerts if a.get("sentiment") in ("BEARISH","PUT"))
        tot  = bull + bear
        batch_flow[ticker] = {
            "flow_score": round((bull-bear)/tot*100, 1) if tot else 0,
            "sweeps":     sum(1 for a in alerts if a.get("is_sweep")),
        }
    batch_dp = {}
    for ticker, prints in dp_by.items():
        buys  = sum(1 for d in prints if d.get("side") in ("BUY","A"))
        sells = sum(1 for d in prints if d.get("side") in ("SELL","B"))
        tot   = buys + sells
        batch_dp[ticker] = {
            "dp_score": round((buys-sells)/tot*100, 1) if tot else 0,
        }

    print(f"[SmartEngine] Shared data in {time.time()-t0:.1f}s | "
          f"VIX={vix.get('current')} | news={len(global_news)} | "
          f"earnings={len(earnings_pre+earnings_post)}")

    # ── Scanner (skip if pre-scanned picks provided) ──────────────────────────
    if pre_scanned:
        picks = pre_scanned
        print(f"[SmartEngine] Using {len(picks)} pre-scanned picks")
    else:
        print(f"[SmartEngine] Running scanner...")
        t0      = time.time()
        tickers = get_scan_universe(user_id=user_id)
        picks   = quick_scan(tickers, user_id=user_id, top_n=top_picks)

    # Filter to optionable tickers
    candidates = [p for p in picks if _is_optionable(p)]
    print(f"[SmartEngine] Scanner: {len(picks)} picks → {len(candidates)} optionable in {time.time()-t0:.1f}s")

    if not candidates:
        # Fallback to liquid tickers
        candidates = [
            {"ticker": "SPY", "direction": "BEARISH", "price": 590, "confidence": 60,
             "change_pct": 0, "flow_score": 0, "dp_score": 0, "sweeps": 0, "alert_count": 0, "score": 0.5, "signals": []},
            {"ticker": "QQQ", "direction": "BEARISH", "price": 510, "confidence": 60,
             "change_pct": 0, "flow_score": 0, "dp_score": 0, "sweeps": 0, "alert_count": 0, "score": 0.5, "signals": []},
        ]
        print("[SmartEngine] No candidates — using SPY/QQQ fallback")

    # ── Phase 1: Parallel enrichment ──────────────────────────────────────────
    print(f"[SmartEngine] Enriching {len(candidates)} candidates in parallel...")
    t0       = time.time()
    enriched = []

    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
        futures = {
            ex.submit(_enrich_ticker, c, earnings_map, batch_flow, batch_dp): c["ticker"]
            for c in candidates
        }
        for future in as_completed(futures, timeout=30):
            try:
                enriched.append(future.result())
            except Exception as e:
                print(f"[SmartEngine] Enrich failed: {e}")

    print(f"[SmartEngine] Enriched {len(enriched)} tickers in {time.time()-t0:.1f}s")

    # Sort by signal strength
    enriched.sort(key=lambda x: (
        abs(x.get("flow_score", 0)) +
        abs(x.get("dp_score", 0)) +
        abs(x.get("change_pct", 0)) * 2 +
        (20 if x.get("earnings_days", 999) < 14 else 0)
    ), reverse=True)

    # ── Phase 2: Single LLM call ──────────────────────────────────────────────
    print(f"[SmartEngine] Calling LLM with {len(enriched)} candidates...")
    t0     = time.time()
    prompt = _build_llm_prompt(enriched[:10], vix, global_news, budget, today)
    llm_result = _call_smart_llm(prompt)
    print(f"[SmartEngine] LLM responded in {time.time()-t0:.1f}s")

    if not llm_result:
        return {
            "error":   "LLM call failed",
            "elapsed": round(time.time()-t_total, 1),
        }

    print(f"[SmartEngine] Market view: {llm_result.get('market_view','')}")
    print(f"[SmartEngine] Recommendations: {len(llm_result.get('recommendations',[]))}")

    # ── Phase 3: Deterministic math ───────────────────────────────────────────
    t0       = time.time()
    final    = []
    for rec in (llm_result.get("recommendations") or []):
        trade = _execute_smart_rec(rec, budget, user_id)
        if trade:
            final.append(trade)
            print(f"[SmartEngine] ✅ {rec['ticker']} {rec['direction']} "
                  f"{rec['strategy']} exp={rec['expiry']} conf={rec.get('confidence')}")
        else:
            print(f"[SmartEngine] ⚠️  {rec.get('ticker')} math failed")

    print(f"[SmartEngine] Math done in {time.time()-t0:.1f}s")

    # ── Stock recommendations (parallel, no LLM needed for fundamentals) ──────
    stock_recs = []
    try:
        from app.recommendations.horizon_engine import get_stock_for_horizon
        import yfinance as yf
        stock_tickers = ["NVDA", "AAPL", "MSFT"]  # top liquid from watchlist
        for t in stock_tickers[:2]:
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

    # Store option recs to daily_recommendations
    market_view = llm_result.get("market_view", "")
    for rec in final:
        try:
            from app.recommendations.daily_engine import _upsert_recommendation
            legs = rec.get("legs", [])
            _upsert_recommendation(user_id, {
                "ticker":           rec["ticker"],
                "horizon":          rec.get("horizon", "17d"),
                "direction":        rec["direction"],
                "conviction_score": rec.get("confidence", 65),
                "conviction_tier":  "HIGH" if rec.get("confidence",0)>=75
                                    else "MODERATE" if rec.get("confidence",0)>=65
                                    else "WATCH",
                "act_now":          rec.get("confidence", 0) >= 70,
                "position_size_guidance": "standard",
                "thesis":           rec.get("reasoning", ""),
                "entry_zone_low":   abs(rec.get("entry_debit", 0)),
                "entry_zone_high":  abs(rec.get("entry_debit", 0)) * 1.05,
                "entry_trigger":    "AT_MARKET",
                "target_price":     0,
                "target_pct":       rec.get("max_profit_per_contract", 0) /
                                    max(abs(rec.get("max_loss_per_contract", 100)), 1) * 100,
                "stop_price":       0,
                "stop_pct":         -40.0,
                "timeframe":        f"{rec.get('dte', 17)} days",
                "invalidation_conditions": rec.get("key_risk", ""),
                "strategy":         rec.get("strategy", ""),
                "expiry":           rec.get("expiry", ""),
                "dte":              rec.get("dte", 17),
                "legs":             legs,
                "entry_debit":      rec.get("entry_debit", 0),
                "total_cost":       rec.get("total_cost", 0),
                "max_profit":       rec.get("max_profit_per_contract", 0),
                "max_loss":         rec.get("max_loss_per_contract", 0),
                "risk_reward":      rec.get("risk_reward", 0),
                "webull_instructions": rec.get("webull_instructions", ""),
                "key_news":         rec.get("catalyst", "NONE"),
                "warnings":         [],
                "conviction_breakdown": {},
                "signal_data":      {"market_view": market_view},
            })
            print(f"[SmartEngine] Stored rec: {rec['ticker']}")
        except Exception as e:
            print(f"[SmartEngine] Store failed for {rec.get('ticker','?')}: {e}")

    total_time = round(time.time()-t_total, 1)
    print(f"\n[SmartEngine] COMPLETE in {total_time}s — "
          f"{len(final)} option recs + {len(stock_recs)} stock recs")

    return {
        "market_view":       llm_result.get("market_view", ""),
        "options":           final,
        "stocks":            stock_recs,
        "skipped":           llm_result.get("skip", []),
        "skip_reason":       llm_result.get("skip_reason", ""),
        "candidates_scanned": len(candidates),
        "elapsed":           total_time,
        "vix":               vix.get("current"),
        "vix_zone":          vix.get("zone"),
        "date":              today,
    }
