"""
Rescan with Validation — keeps picks consistent through the day.

Flow:
  1. Load morning picks from daily_recommendations
  2. Get fresh signals for same tickers + watchlist
  3. LLM evaluates each morning pick: INTACT / UPDATED / BROKEN
  4. LLM finds NEW picks from remaining candidates
  5. Merge: sort by conviction descending
  6. Result: stable picks unless market proves them wrong
"""
import json
import time
from datetime import datetime


STATUS_INTACT  = "INTACT"   # thesis still valid
STATUS_UPDATED = "UPDATED"  # stronger now, higher conviction
STATUS_BROKEN  = "BROKEN"   # proven wrong, drop it
STATUS_NEW     = "NEW"      # fresh pick from rescan


def _load_todays_recs(user_id: str) -> list[dict]:
    """Load today's active recommendations from DB."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT id, ticker, direction, strategy, expiry, dte,
                       conviction_score, thesis, webull_instructions,
                       legs, entry_debit, max_profit, max_loss,
                       risk_reward, invalidation_conditions, key_news,
                       signal_data, created_at
                FROM daily_recommendations
                WHERE user_id=:uid AND date=CURRENT_DATE AND status='ACTIVE'
                ORDER BY conviction_score DESC
            """), {"uid": user_id}).fetchall()

        picks = []
        for r in rows:
            sd = r.signal_data or {}
            picks.append({
                "id":                str(r.id),
                "ticker":            r.ticker,
                "direction":         r.direction,
                "strategy":          r.strategy or "",
                "expiry":            str(r.expiry) if r.expiry else "",
                "dte":               r.dte or 17,
                "conviction":        r.conviction_score or 65,
                "thesis":            r.thesis or "",
                "webull_instructions": r.webull_instructions or "",
                "legs":              r.legs or [],
                "entry_debit":       float(r.entry_debit or 0),
                "max_profit":        float(r.max_profit or 0),
                "max_loss":          float(r.max_loss or 0),
                "risk_reward":       float(r.risk_reward or 0),
                "invalidation":      r.invalidation_conditions or "",
                "key_news":          r.key_news or "",
                "market_view":       sd.get("market_view", ""),
                "status":            STATUS_INTACT,  # default until LLM evaluates
                "status_reason":     "",
                "scan_time":         str(r.created_at)[:16] if r.created_at else "",
            })
        return picks
    except Exception as e:
        print(f"[Rescan] Could not load today's recs: {e}")
        return []


def _format_morning_pick(pick: dict) -> str:
    """Compress morning pick into ~80 tokens for LLM context."""
    legs = pick.get("legs", [])
    strike_str = " / ".join(
        f"{l.get('action','')} ${l.get('strike',0):.0f}{l.get('type','')[0]}"
        for l in legs
    ) if legs else ""

    return (
        f"[{pick['ticker']}] {pick['direction']} {pick['strategy'].replace('_',' ')} "
        f"{strike_str} exp={pick['expiry']} conf={pick['conviction']}\n"
        f"  Thesis: {pick['thesis'][:120]}\n"
        f"  Breaks if: {pick['invalidation'][:80]}"
    )


def _build_validation_prompt(
    morning_picks: list[dict],
    fresh_candidates: list[dict],
    vix: dict,
    global_news: list[dict],
    budget: float,
    today: str,
) -> str:
    """Build single LLM prompt that validates morning picks + finds new ones."""

    # Morning picks section
    if morning_picks:
        mp_block = "\n\n".join(_format_morning_pick(p) for p in morning_picks)
        morning_section = f"""=== MORNING PICKS — VALIDATE EACH ===
{mp_block}

For each morning pick above, decide:
INTACT  → thesis still holds, small price moves are noise, keep same strikes
UPDATED → signal got STRONGER (better flow, bigger move in right direction), raise conviction
BROKEN  → price moved significantly AGAINST thesis OR thesis condition violated → drop it
"""
    else:
        morning_section = "=== NO MORNING PICKS YET — FIND BEST PICKS ==="

    # Fresh candidates section
    from app.recommendations.smart_engine import _compress_ticker
    candidates_block = "\n\n".join(
        _compress_ticker(c) for c in fresh_candidates[:10]
    )

    # News
    news_str = "\n".join(
        f"  - {n.get('headline','')[:80]}"
        for n in (global_news or [])[:5]
    )

    return f"""You are managing real money. Today is {today}. Budget: ${budget:.0f}.

MARKET: VIX {vix.get('current', 17)} ({vix.get('zone','NORMAL')}) | {vix.get('trend','STABLE')}
NEWS: {news_str}

{morning_section}

=== FRESH CANDIDATES (new opportunities) ===
{candidates_block}

=== YOUR TASK ===
1. Validate each morning pick (INTACT/UPDATED/BROKEN)
2. Find NEW picks from fresh candidates (only if not overlapping INTACT picks)
3. Return single merged list ranked by conviction (highest first)

STRIKE RULES:
- Debit call spread: buy_strike LOWER than sell_strike (e.g. buy $62C sell $65C)
- Debit put spread: buy_strike HIGHER than sell_strike (e.g. buy $61P sell $58P)
- Max 5% OTM from current price — no further

Respond in valid JSON only:
{{
  "market_view": "one sentence on current market bias",
  "picks": [
    {{
      "ticker": "AFRM",
      "status": "INTACT",
      "status_reason": "2% dip is noise, RSI still 66, flow unchanged",
      "direction": "BULLISH",
      "strategy": "DEBIT_CALL_SPREAD",
      "expiry": "2026-07-17",
      "buy_strike": 63.0,
      "sell_strike": 66.0,
      "reasoning": "Breaking resistance with bullish flow",
      "key_risk": "Close below $61",
      "catalyst": "Momentum + bullish dark pool",
      "confidence": 78
    }}
  ]
}}"""


def rescan_with_validation(
    user_id:   str,
    budget:    float = 2000.0,
    pre_scanned: list | None = None,
    sector:    str | None = None,
    cap_size:  str | None = None,
    catalyst:  str | None = None,
) -> dict:
    """
    Rescan with morning picks validation.
    Keeps picks consistent — only changes if market proves them wrong.
    """
    from app.recommendations.smart_engine import (
        _build_earnings_map, _enrich_ticker, _call_smart_llm,
        _execute_smart_rec, _is_optionable,
    )
    from app.options_flow.unusual_whales import (
        get_earnings_premarket, get_earnings_afterhours,
        get_flow_alerts, get_dark_pool_recent,
    )
    from app.rag.context_builder import _build_vix_context, _build_global_news
    from app.scanner.quick_scan import quick_scan
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t_start = time.time()
    today   = datetime.now().strftime("%A %B %d, %Y")

    # ── Step 1: Load morning picks ────────────────────────────────────────────
    morning_picks = _load_todays_recs(user_id)
    print(f"[Rescan] {len(morning_picks)} morning picks loaded")

    # ── Step 2: Get filtered universe ─────────────────────────────────────────
    if sector and cap_size:
        from app.recommendations.filtered_universe import get_filtered_universe
        tickers = get_filtered_universe(sector, cap_size, catalyst or "any", user_id)
        print(f"[Rescan] Filtered universe: {len(tickers)} tickers ({sector}/{cap_size}/{catalyst})")
    elif pre_scanned:
        tickers = [p["ticker"] for p in pre_scanned]
    else:
        from app.scanner.universe import get_scan_universe
        tickers = get_scan_universe(user_id=user_id)

    # ── Step 3: Shared data (parallel) ───────────────────────────────────────
    vix         = _build_vix_context()
    global_news = _build_global_news()
    earnings_pre  = get_earnings_premarket()  or []
    earnings_post = get_earnings_afterhours() or []
    earnings_map  = _build_earnings_map(earnings_pre, earnings_post)

    all_flow = get_flow_alerts(limit=500)     or []
    all_dp   = get_dark_pool_recent(limit=500) or []
    flow_by  = {}
    for a in all_flow: flow_by.setdefault(a.get("ticker",""), []).append(a)
    dp_by = {}
    for d in all_dp:   dp_by.setdefault(d.get("ticker",""),  []).append(d)

    batch_flow = {}
    for ticker, alerts in flow_by.items():
        bull = sum(1 for a in alerts if a.get("sentiment") in ("BULLISH","CALL"))
        bear = sum(1 for a in alerts if a.get("sentiment") in ("BEARISH","PUT"))
        tot  = bull + bear
        batch_flow[ticker] = {
            "flow_score": round((bull-bear)/tot*100,1) if tot else 0,
            "sweeps": sum(1 for a in alerts if a.get("is_sweep")),
        }
    batch_dp = {}
    for ticker, prints in dp_by.items():
        buys  = sum(1 for d in prints if d.get("side") in ("BUY","A"))
        sells = sum(1 for d in prints if d.get("side") in ("SELL","B"))
        tot   = buys + sells
        batch_dp[ticker] = {"dp_score": round((buys-sells)/tot*100,1) if tot else 0}

    # ── Step 4: Scan + enrich ─────────────────────────────────────────────────
    if pre_scanned:
        picks = pre_scanned
    else:
        picks = quick_scan(tickers, user_id=user_id, top_n=15)

    candidates = [p for p in picks if _is_optionable(p)][:12]

    enriched = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(_enrich_ticker, c, earnings_map, batch_flow, batch_dp): c["ticker"]
            for c in candidates
        }
        for future in as_completed(futures, timeout=20):
            try: enriched.append(future.result())
            except Exception: pass

    enriched.sort(
        key=lambda x: abs(x.get("flow_score",0)) + abs(x.get("dp_score",0)) + abs(x.get("change_pct",0))*2,
        reverse=True
    )
    print(f"[Rescan] Enriched {len(enriched)} candidates in {time.time()-t_start:.1f}s")

    # ── Step 5: Single LLM call (validation + new picks) ─────────────────────
    prompt     = _build_validation_prompt(morning_picks, enriched, vix, global_news, budget, today)
    llm_result = _call_smart_llm(prompt)

    if not llm_result:
        # LLM failed — return morning picks unchanged
        return {
            "picks":       morning_picks,
            "market_view": "",
            "source":      "morning_cache",
            "elapsed":     round(time.time()-t_start, 1),
        }

    print(f"[Rescan] LLM responded with {len(llm_result.get('picks',[]))} picks")

    # ── Step 6: Execute math for NEW/UPDATED picks, keep INTACT as-is ────────
    final = []
    morning_by_ticker = {p["ticker"]: p for p in morning_picks}

    for llm_pick in (llm_result.get("picks") or []):
        ticker = llm_pick.get("ticker","")
        status = llm_pick.get("status", STATUS_NEW)

        if status == STATUS_BROKEN:
            # Keep in list but mark broken so user can see what changed
            mp = morning_by_ticker.get(ticker, {})
            final.append({
                **mp,
                "status":        STATUS_BROKEN,
                "status_reason": llm_pick.get("status_reason","Thesis broken"),
                "confidence":    0,
                "conviction_score": 0,
            })
            continue

        if status == STATUS_INTACT and ticker in morning_by_ticker:
            # Keep exact morning pick — just update status reason
            mp = morning_by_ticker[ticker].copy()
            mp["status"]        = STATUS_INTACT
            mp["status_reason"] = llm_pick.get("status_reason","Thesis intact")
            mp["confidence"]    = llm_pick.get("confidence", mp.get("conviction",65))
            final.append(mp)
            continue

        # UPDATED or NEW — run fresh math
        from app.recommendations.smart_engine import _execute_smart_rec
        trade = _execute_smart_rec(llm_pick, budget, user_id)
        if trade:
            trade["status"]        = status
            trade["status_reason"] = llm_pick.get("status_reason","")
            trade["confidence"]    = llm_pick.get("confidence", 65)
            final.append(trade)

            # Store NEW picks to DB
            if status == STATUS_NEW:
                try:
                    from app.recommendations.daily_engine import _upsert_recommendation
                    legs = trade.get("legs", [])
                    market_view = llm_result.get("market_view","")
                    _upsert_recommendation(user_id, {
                        "ticker": ticker, "horizon": trade.get("horizon","17d"),
                        "direction": trade.get("direction",""), "conviction_score": trade.get("confidence",65),
                        "conviction_tier": "HIGH" if trade.get("confidence",0)>=75 else "MODERATE",
                        "act_now": trade.get("confidence",0)>=70,
                        "position_size_guidance": "standard",
                        "thesis": llm_pick.get("reasoning",""),
                        "entry_zone_low": abs(trade.get("entry_debit",0)),
                        "entry_zone_high": abs(trade.get("entry_debit",0))*1.05,
                        "entry_trigger": "AT_MARKET",
                        "target_price": 0, "target_pct": 0,
                        "stop_price": 0, "stop_pct": -40.0,
                        "timeframe": f"{trade.get('dte',17)} days",
                        "invalidation_conditions": llm_pick.get("key_risk",""),
                        "strategy": trade.get("strategy",""),
                        "expiry": trade.get("expiry",""), "dte": trade.get("dte",17),
                        "legs": legs, "entry_debit": trade.get("entry_debit",0),
                        "total_cost": trade.get("total_cost",0),
                        "max_profit": trade.get("max_profit_per_contract",0),
                        "max_loss": trade.get("max_loss_per_contract",0),
                        "risk_reward": trade.get("risk_reward",0),
                        "webull_instructions": trade.get("webull_instructions",""),
                        "key_news": llm_pick.get("catalyst","NONE"),
                        "warnings": [], "conviction_breakdown": {},
                        "signal_data": {"market_view": market_view},
                    })
                except Exception as e:
                    print(f"[Rescan] Store failed {ticker}: {e}")

    # Sort: UPDATED first, then INTACT by conviction, BROKEN last
    status_order = {STATUS_UPDATED: 0, STATUS_NEW: 1, STATUS_INTACT: 2, STATUS_BROKEN: 99}
    final.sort(key=lambda x: (
        status_order.get(x.get("status", STATUS_INTACT), 2),
        -(x.get("confidence") or x.get("conviction",0))
    ))

    elapsed = round(time.time()-t_start, 1)
    print(f"[Rescan] COMPLETE in {elapsed}s — {len(final)} picks")

    return {
        "picks":       final,
        "market_view": llm_result.get("market_view",""),
        "source":      "rescan_validated",
        "elapsed":     elapsed,
        "vix":         vix.get("current"),
        "date":        today,
    }
