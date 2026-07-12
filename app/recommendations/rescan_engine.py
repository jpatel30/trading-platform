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


def _load_todays_recs(user_id: str, horizon: str = "", min_exp: str = "", max_exp: str = "") -> list[dict]:
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
                  AND strategy != 'STOCK'
                  AND legs IS NOT NULL AND jsonb_array_length(legs) > 0
                  AND (:horizon = '' OR horizon = :horizon)
                  AND (:min_exp = '' OR expiry >= CAST(:min_exp AS date))
                  AND (:max_exp = '' OR expiry <= CAST(:max_exp AS date))
                ORDER BY conviction_score DESC
            """), {
                "uid": user_id,
                "horizon": horizon,
                "min_exp": min_exp,
                "max_exp": max_exp,
            }).fetchall()

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
                "confidence":        r.conviction_score or 65,
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
    regime: dict | None = None,
    horizon: str = "1w",
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

    regime_str = ""
    if regime:
        ts  = regime.get("vix_structure", {})
        pcr = regime.get("put_call", {})
        regime_str = (
            f"\nVIX TERM STRUCTURE: VIX9D={ts.get('vix9d',0):.1f} vs VIX={ts.get('vix30',0):.1f} "
            f"({ts.get('signal','?')}) | PCR={pcr.get('pcr',0):.2f} ({pcr.get('signal','?')})"
            f"\nOVERALL BIAS: {regime.get('overall_bias','?')} — {regime.get('strategy_hint','')}"
        )

    # Horizon guidance for LLM
    _horizon_labels = {"1w":"1 WEEK","2w":"2 WEEKS","1m":"1 MONTH","3m":"3 MONTHS","6m":"6 MONTHS"}
    from datetime import date, timedelta
    _today_dt = date.today()
    _expiry_guidance = {
        "1w":  f"MUST use expiry between {(_today_dt+timedelta(3)).strftime('%Y-%m-%d')} and {(_today_dt+timedelta(7)).strftime('%Y-%m-%d')}",
        "2w":  f"MUST use expiry between {(_today_dt+timedelta(8)).strftime('%Y-%m-%d')} and {(_today_dt+timedelta(14)).strftime('%Y-%m-%d')}",
        "1m":  f"MUST use expiry between {(_today_dt+timedelta(21)).strftime('%Y-%m-%d')} and {(_today_dt+timedelta(45)).strftime('%Y-%m-%d')}",
        "3m":  f"MUST use expiry between {(_today_dt+timedelta(60)).strftime('%Y-%m-%d')} and {(_today_dt+timedelta(90)).strftime('%Y-%m-%d')}",
        "6m":  f"MUST use expiry between {(_today_dt+timedelta(120)).strftime('%Y-%m-%d')} and {(_today_dt+timedelta(180)).strftime('%Y-%m-%d')}",
    }
    horizon_label   = _horizon_labels.get(horizon or "1w", "SHORT TERM")
    expiry_guidance = _expiry_guidance.get(horizon or "1w", "Pick appropriate expiry from candidate list.")

    # Example expiry for JSON schema — use midpoint of the horizon range
    from datetime import date as _d2, timedelta as _td2
    _example_offsets = {"1w":5,"2w":11,"1m":30,"3m":75,"6m":150}
    _example_expiry  = (_d2.today() + _td2(_example_offsets.get(horizon or "1w", 30))).strftime("%Y-%m-%d")

    return f"""You are managing real money. Today is {today}. Budget: ${budget:.0f}.

MARKET: VIX {vix.get('current', 17)} ({vix.get('zone','NORMAL')}) | {vix.get('trend','STABLE')}{regime_str}
NEWS: {news_str}

{morning_section}

=== FRESH CANDIDATES (new opportunities) ===
{candidates_block}

=== YOUR TASK ===
1. Validate each morning pick (INTACT/UPDATED/BROKEN)
2. REQUIRED: Always include SPY (this week expiry) and QQQ (next week expiry) in new_picks
   - Decide direction based on VIX trend + market flow
   - Pick best strategy: NAKED_CALL, NAKED_PUT, DEBIT_CALL_SPREAD, DEBIT_PUT_SPREAD, STRADDLE, STRANGLE
   - If VIX FALLING + bullish flow → CALL strategy; if VIX RISING → PUT or STRADDLE
3. Find additional NEW picks from fresh candidates
4. Return morning_status for existing picks + new_picks ranked by conviction

⚠️  USE EXACT PRICES SHOWN ABOVE — do NOT use your training data prices.
SPY trades around $750, QQQ around $720 as of today. Use the price shown in brackets.

HORIZON: {horizon_label}
{expiry_guidance}

STRATEGY SELECTION (MANDATORY — pick based on conditions):
  VIX STEEP_CONTANGO + NEUTRAL regime → IRON_CONDOR preferred (sell both sides, collect premium)
  Strong directional flow + conviction >80 → NAKED_CALL or NAKED_PUT
  Moderate directional + IV rank <50 → DEBIT_CALL_SPREAD or DEBIT_PUT_SPREAD
  Direction unclear + big move expected → STRADDLE or STRANGLE
  SPY/QQQ in NEUTRAL market → IRON_CONDOR is best fit

STRIKE RULES:
- Debit call spread: buy_strike LOWER than sell_strike (e.g. buy $750C sell $760C)
- Debit put spread: buy_strike HIGHER than sell_strike (e.g. buy $750P sell $740P)
- Naked call/put: buy_strike = ATM or slightly OTM, sell_strike = 0
- Iron condor: buy_strike = lower put, sell_strike = upper call (wider = safer)
- SPREAD WIDTH: 1W=$2-5, 2W=$5-10, 1M=$10-20, 3M=$20-40
- Max 8% OTM from current price

Respond ONLY with compact JSON — no prose, no markdown:
{{
  "market_view": "one sentence",
  "morning_status": {{
    "TICKER": {{"status": "INTACT", "reason": "5 words", "confidence": 75}}
  }},
  "new_picks": [
    {{"ticker": "SPY", "direction": "NEUTRAL", "strategy": "IRON_CONDOR",
     "expiry": "{_example_expiry}", "buy_strike": 730.0, "sell_strike": 770.0,
     "reasoning": "brief", "key_risk": "brief", "confidence": 72}},
    {{"ticker": "X", "direction": "BULLISH", "strategy": "NAKED_CALL",
     "expiry": "{_example_expiry}", "buy_strike": 0.0, "sell_strike": 0.0,
     "reasoning": "brief", "key_risk": "brief", "confidence": 70}}
  ]
}}"""


def rescan_with_validation(
    user_id:   str,
    budget:    float = 2000.0,
    pre_scanned: list | None = None,
    sector:    str | None = None,
    cap_size:  str | None = None,
    catalyst:  str | None = None,
    horizon:   str = "",
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
    from app.signals.market_regime import get_full_market_regime
    from app.scanner.quick_scan import quick_scan
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t_start = time.time()
    today   = datetime.now().strftime("%A %B %d, %Y")

    from app.utils.scan_status import set_scan_status, clear_scan_status
    set_scan_status(user_id, "queued")

    # ── Step 1: Load morning picks ────────────────────────────────────────────
    # Normalize horizon: UI sends 1w/2w/1m, DB stores 1w/2w/1m or 17d/30d
    _horizon_map = {"1w":"1w","2w":"2w","1m":"1m","3m":"3m","6m":"6m",
                    "17d":"1w","30d":"1m","21d":"2w","90d":"3m","180d":"6m"}
    _norm_horizon = _horizon_map.get(horizon, horizon) if horizon else ""

    # Expiry range for morning picks — prevents loading wrong-expiry picks
    from datetime import date as _date, timedelta as _td
    _today_d = _date.today()
    _expiry_ranges = {
        "1w":  (_today_d + _td(1),   _today_d + _td(9)),
        "2w":  (_today_d + _td(8),   _today_d + _td(16)),
        "1m":  (_today_d + _td(18),  _today_d + _td(50)),
        "3m":  (_today_d + _td(55),  _today_d + _td(100)),
        "6m":  (_today_d + _td(110), _today_d + _td(200)),
    }
    _range = _expiry_ranges.get(_norm_horizon, (None, None))
    _min_exp = _range[0].strftime("%Y-%m-%d") if _range[0] else ""
    _max_exp = _range[1].strftime("%Y-%m-%d") if _range[1] else ""

    morning_picks = _load_todays_recs(user_id, horizon=_norm_horizon, min_exp=_min_exp, max_exp=_max_exp)
    print(f"[Rescan] {len(morning_picks)} morning picks loaded")
    from app.utils.scan_status import set_scan_status
    set_scan_status(user_id, "prices")

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
    regime      = get_full_market_regime()
    print(f"[Rescan] Market regime: {regime['overall_bias']} | {regime['summary']}")
    set_scan_status(user_id, "regime")
    earnings_pre  = get_earnings_premarket()  or []
    earnings_post = get_earnings_afterhours() or []
    earnings_map  = _build_earnings_map(earnings_pre, earnings_post)

    all_flow = get_flow_alerts(limit=500)        or []
    all_dp   = get_dark_pool_recent(limit=200)   or []   # max 200 for dp
    flow_by  = {}
    for a in all_flow: flow_by.setdefault(a.get("ticker",""), []).append(a)
    dp_by = {}
    for d in all_dp:   dp_by.setdefault(d.get("ticker",""),  []).append(d)

    batch_flow = {}
    for ticker, alerts in flow_by.items():
        bull = sum(1 for a in alerts if a.get("type","").lower() == "call"
                   or a.get("sentiment","").upper() in ("BULLISH","CALL"))
        bear = sum(1 for a in alerts if a.get("type","").lower() == "put"
                   or a.get("sentiment","").upper() in ("BEARISH","PUT"))
        tot  = bull + bear
        batch_flow[ticker] = {
            "flow_score": round((bull-bear)/tot*100,1) if tot else 0,
            "sweeps": sum(1 for a in alerts if a.get("is_sweep")),
        }
    batch_dp = {}
    for ticker, prints in dp_by.items():

        def _dp_side(d):
            try:
                price = float(d.get("price",0) or 0)
                ask   = float(d.get("nbbo_ask",0) or 0)
                bid   = float(d.get("nbbo_bid",0) or 0)
                if ask and price >= ask * 0.999: return "BUY"
                if bid and price <= bid * 1.001: return "SELL"
                return d.get("side","")
            except Exception:
                return d.get("side","")
        buys  = sum(1 for d in prints if _dp_side(d) in ("BUY","A"))
        sells = sum(1 for d in prints if _dp_side(d) in ("SELL","B"))
        tot   = buys + sells
        batch_dp[ticker] = {"dp_score": round((buys-sells)/tot*100,1) if tot else 0}

    # Alphabetical rotation — full 127-ticker UW coverage across 3 scans
    from datetime import datetime as _dt
    wl_sorted      = sorted(set(tickers))
    batch_idx      = (_dt.now().minute // 20) % 3
    rotation_slice = wl_sorted[batch_idx*43:(batch_idx+1)*43]
    uncovered      = [t for t in rotation_slice if t not in batch_flow and t not in batch_dp][:15]
    if uncovered:
        print(f"[Rescan] Per-ticker UW for {len(uncovered)} uncovered (batch {batch_idx}): {uncovered[:5]}...")
        from concurrent.futures import ThreadPoolExecutor, as_completed as _ac
        def _tf(t):
            try:
                from app.options_flow.unusual_whales import get_flow_alerts as _fa, get_dark_pool_ticker as _dp
                return t, _fa(ticker=t, limit=20) or [], _dp(t, limit=20) or []
            except Exception:
                return t, [], []
        with ThreadPoolExecutor(max_workers=5) as ex:
            for fut in _ac({ex.submit(_tf, t): t for t in uncovered}, timeout=30):
                try:
                    t, tf, td = fut.result()
                    if tf:
                        bull = sum(1 for a in tf if a.get("sentiment") in ("BULLISH","CALL"))
                        bear = sum(1 for a in tf if a.get("sentiment") in ("BEARISH","PUT"))
                        tot  = bull + bear
                        batch_flow[t] = {"flow_score": round((bull-bear)/tot*100,1) if tot else 0,
                                         "sweeps": sum(1 for a in tf if a.get("is_sweep"))}
                    if td:
                        buys  = sum(1 for d in td if d.get("side") in ("BUY","A"))
                        sells = sum(1 for d in td if d.get("side") in ("SELL","B"))
                        tot   = buys + sells
                        batch_dp[t] = {"dp_score": round((buys-sells)/tot*100,1) if tot else 0}
                except Exception:
                    pass
        print(f"[Rescan] Total UW coverage: {len(batch_flow)} flow + {len(batch_dp)} dp tickers")


    # ── Step 4: Scan + enrich ─────────────────────────────────────────────────
    if pre_scanned:
        picks = pre_scanned
    else:
        picks = quick_scan(tickers, user_id=user_id, top_n=15)


    # Always include SPY + QQQ with next expiry dates
    def _next_friday(weeks=0):
        from datetime import date, timedelta
        today = date.today()
        days = (4 - today.weekday()) % 7 or 7
        return (today + timedelta(days=days + weeks*7)).strftime("%Y-%m-%d")

    # Get live prices for SPY/QQQ
    import yfinance as _yf
    _spy_p = _yf.Ticker("SPY").fast_info.last_price or 0
    _qqq_p = _yf.Ticker("QQQ").fast_info.last_price or 0

    # Expiry based on horizon
    _horizon_weeks = {"1w":0,"2w":1,"1m":3,"3m":8,"6m":20}
    _idx_weeks = _horizon_weeks.get(_norm_horizon or "1w", 1)
    _spy_exp = _next_friday(_idx_weeks)
    _qqq_exp = _next_friday(_idx_weeks + 1)

    index_candidates = [
        {"ticker": "SPY", "direction": "UNKNOWN", "price": _spy_p, "confidence": 75,
         "change_pct": 0, "flow_score": 0, "dp_score": 0, "sweeps": 0,
         "alert_count": 0, "score": 1.0, "signals": ["index"],
         "forced_expiry": _spy_exp},
        {"ticker": "QQQ", "direction": "UNKNOWN", "price": _qqq_p, "confidence": 75,
         "change_pct": 0, "flow_score": 0, "dp_score": 0, "sweeps": 0,
         "alert_count": 0, "score": 1.0, "signals": ["index"],
         "forced_expiry": _qqq_exp},
    ]
    print(f"[Rescan] SPY=${_spy_p:.2f} exp={_spy_exp} | QQQ=${_qqq_p:.2f} exp={_qqq_exp} (horizon={_norm_horizon})")
    set_scan_status(user_id, "enriching")
    # Lock in live prices for index picks — don't let enrichment overwrite them
    for ip in index_candidates:
        ip["_locked_price"] = ip["price"]

    # Prepend index picks, then fill remaining slots from scanner
    other_picks = [p for p in picks if p["ticker"] not in ("SPY","QQQ") and _is_optionable(p)]
    candidates = index_candidates + other_picks[:10]

    enriched = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(_enrich_ticker, c, earnings_map, batch_flow, batch_dp): c["ticker"]
            for c in candidates
        }
        for future in as_completed(futures, timeout=90):
            try: enriched.append(future.result())
            except Exception: pass

    enriched.sort(
        key=lambda x: abs(x.get("flow_score",0)) + abs(x.get("dp_score",0)) + abs(x.get("change_pct",0))*2,
        reverse=True
    )
    print(f"[Rescan] Enriched {len(enriched)} candidates in {time.time()-t_start:.1f}s")
    set_scan_status(user_id, "llm_thinking")

    # ── Step 5: Single LLM call (validation + new picks) ─────────────────────
    prompt     = _build_validation_prompt(morning_picks, enriched, vix, global_news, budget, today, regime=regime, horizon=_norm_horizon or "1w")
    llm_result = _call_smart_llm(prompt)
    set_scan_status(user_id, "llm_done")

    if not llm_result:
        # LLM failed — return morning picks but still filter to options only
        option_picks = [p for p in morning_picks if p.get("legs") and len(p.get("legs",[])) > 0]
        print(f"[Rescan] LLM failed — returning {len(option_picks)} cached option picks (filtered from {len(morning_picks)})")
        return {
            "picks":       option_picks,
            "market_view": "",
            "source":      "morning_cache_fallback",
            "elapsed":     round(time.time()-t_start, 1),
        }

    # ── Step 6: Execute math for NEW/UPDATED picks, keep INTACT as-is ────────
    final = []
    morning_by_ticker = {p["ticker"]: p for p in morning_picks}
    print(f"[Rescan] morning_by_ticker: {list(morning_by_ticker.keys())}")

    # ── Parse compact format: morning_status + new_picks ─────────────────
    morning_status = llm_result.get("morning_status", {})
    new_picks_llm  = llm_result.get("new_picks", [])
    market_view    = llm_result.get("market_view", "")

    # Fallback: old picks[] format
    if not morning_status and not new_picks_llm:
        new_picks_llm = llm_result.get("picks", [])
        # Map old format to new_picks format
        new_picks_llm = [p for p in new_picks_llm if p.get("status") == STATUS_NEW
                         or p.get("status") not in (STATUS_INTACT, STATUS_BROKEN, STATUS_UPDATED)]

    # Process morning picks from compact status dict
    for ticker, info in morning_status.items():
        if ticker not in morning_by_ticker:
            continue
        mp     = morning_by_ticker[ticker].copy()
        status = info.get("status", STATUS_INTACT)
        mp["status"]        = status
        mp["status_reason"] = info.get("reason", "")
        mp["confidence"]    = info.get("confidence", mp.get("conviction", 65))
        if status == STATUS_BROKEN:
            mp["confidence"]       = 0
            mp["conviction_score"] = 0
        final.append(mp)

    # Any morning pick not mentioned by LLM → assume INTACT
    for ticker, mp in morning_by_ticker.items():
        if ticker not in morning_status:
            mp = mp.copy()
            mp["status"]        = STATUS_INTACT
            mp["status_reason"] = "No change"
            final.append(mp)

    # Process new picks
    from app.recommendations.smart_engine import _execute_smart_rec
    for llm_pick in new_picks_llm:
        ticker = llm_pick.get("ticker", "")
        if not ticker or ticker in morning_by_ticker:
            continue

        # Map compact fields to _execute_smart_rec format
        rec = {
            "ticker":     ticker,
            "direction":  llm_pick.get("direction", "BULLISH"),
            "strategy":   llm_pick.get("strategy", "DEBIT_CALL_SPREAD"),
            "expiry":     llm_pick.get("expiry", ""),
            "dte":        llm_pick.get("dte", 17),
            "buy_strike": llm_pick.get("buy_strike", 0),
            "sell_strike":llm_pick.get("sell_strike", 0),
            "reasoning":  llm_pick.get("reasoning", ""),
            "key_risk":   llm_pick.get("key_risk", ""),
            "catalyst":   llm_pick.get("catalyst", ""),
            "confidence": llm_pick.get("confidence", 65),
        }

        trade = _execute_smart_rec(rec, budget, user_id)
        if trade:
            trade["status"]        = STATUS_NEW
            trade["status_reason"] = "Fresh pick"
            trade["confidence"]    = rec["confidence"]

            final.append(trade)

            # Store to DB
            try:
                from app.recommendations.daily_engine import _upsert_recommendation
                legs = trade.get("legs", [])
                _upsert_recommendation(user_id, {
                    "ticker": ticker, "horizon": _norm_horizon or trade.get("horizon","1w"),
                    "direction": trade.get("direction",""),
                    "conviction_score": trade.get("confidence",65),
                    "conviction_tier": "HIGH" if trade.get("confidence",0)>=75 else "MODERATE",
                    "act_now": trade.get("confidence",0)>=70,
                    "position_size_guidance": "standard",
                    "thesis": rec.get("reasoning",""),
                    "entry_zone_low": abs(trade.get("entry_debit",0)),
                    "entry_zone_high": abs(trade.get("entry_debit",0))*1.05,
                    "entry_trigger": "AT_MARKET",
                    "target_price": 0, "target_pct": 0,
                    "stop_price": 0, "stop_pct": -40.0,
                    "timeframe": f"{trade.get('dte',17)} days",
                    "invalidation_conditions": rec.get("key_risk",""),
                    "strategy": trade.get("strategy",""),
                    "expiry": trade.get("expiry",""), "dte": trade.get("dte",17),
                    "legs": legs, "entry_debit": trade.get("entry_debit",0),
                    "total_cost": trade.get("total_cost",0),
                    "max_profit": trade.get("max_profit_per_contract",0),
                    "max_loss": trade.get("max_loss_per_contract",0),
                    "risk_reward": trade.get("risk_reward",0),
                    "webull_instructions": trade.get("webull_instructions",""),
                    "key_news": rec.get("catalyst","NONE"),
                    "warnings": [], "conviction_breakdown": {},
                    "signal_data": {"market_view": market_view},
                })
            except Exception as e:
                print(f"[Rescan] Store failed {ticker}: {e}")

    # Filter: only return picks that have legs (options only — no stock leakage)
    final = [p for p in final if p.get("legs") and len(p.get("legs", [])) > 0]

    # Sort: UPDATED first, then INTACT by conviction, BROKEN last
    status_order = {STATUS_UPDATED: 0, STATUS_NEW: 1, STATUS_INTACT: 2, STATUS_BROKEN: 99}
    final.sort(key=lambda x: (
        status_order.get(x.get("status", STATUS_INTACT), 2),
        -(x.get("confidence") or x.get("conviction",0))
    ))

    elapsed = round(time.time()-t_start, 1)
    print(f"[Rescan] COMPLETE in {elapsed}s — {len(final)} picks")

    # Save ALL tickers from this scan to signal_history (free — data already fetched)
    try:
        from sqlalchemy import text as _t
        from app.db.session import get_session as _gs
        _all_picks = list({p["ticker"]: p for p in (pre_scanned or [])}.values())
        _saved = 0
        for _p in _all_picks:
            _tk = _p.get("ticker","")
            if not _tk: continue
            try:
                with _gs() as _s:
                    _s.execute(_t("""
                        INSERT INTO signal_history
                            (user_id,ticker,date,flow_score,dp_score,price,change_pct)
                        VALUES (:uid,:t,CURRENT_DATE,:fs,:dps,:p,:cp)
                        ON CONFLICT (user_id,ticker,date) DO UPDATE SET
                            flow_score=EXCLUDED.flow_score,
                            dp_score=EXCLUDED.dp_score,
                            price=EXCLUDED.price,
                            change_pct=EXCLUDED.change_pct
                    """),{"uid":user_id,"t":_tk,
                          "fs":_p.get("flow_score",0),"dps":_p.get("dp_score",0),
                          "p":_p.get("price",0),"cp":_p.get("change_pct",0)})
                _saved += 1
            except Exception:
                pass
        print(f"[Rescan] Saved {_saved}/{len(_all_picks)} tickers to signal_history")
    except Exception:
        pass

    set_scan_status(user_id, "complete")

    return {
        "picks":       final,
        "market_view": llm_result.get("market_view",""),
        "source":      "rescan_validated",
        "elapsed":     elapsed,
        "vix":         vix.get("current"),
        "date":        today,
    }
