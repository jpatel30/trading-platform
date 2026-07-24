"""
Rescan with Validation — keeps picks consistent through the day.

Flow:
  1. Load morning picks from daily_recommendations (matched by exact
     trading_window_days — a scan run with a different window than an
     earlier one today should NOT reload those as "morning picks")
  2. Get fresh signals for same tickers + watchlist
  3. LLM evaluates each morning pick: INTACT / UPDATED / BROKEN
  4. LLM finds NEW picks from remaining candidates
  5. Merge: sort by conviction descending
  6. Result: stable picks unless market proves them wrong

Rewritten July 2026 (second pass) — replaced the horizon-bucket string
("1w"/"1m"/etc, each with its own hardcoded DTE range) with direct
user inputs: trading_window_days, stop_loss_pct, profit_target_pct.
Strike/strategy selection is UNCHANGED — still driven by flow/TA/IV
signals and the R/R + EV gates built earlier this session. The window
only determines WHICH EXPIRY to target; stop/profit percentages are
applied directly to the stored recommendation (and, via the existing
fill-tracking fix, flow straight through to tracked_positions — the
user's own numbers drive their alerts, not a bucket default).
"""
import json
import time
from datetime import datetime

from app.signals.flow_scoring import compute_flow_score, compute_dp_score
from app.utils.trade_windows import (
    round_budget, validate_trading_window, validate_pct,
    compute_target_date, nearest_friday_to,
)


STATUS_INTACT  = "INTACT"
STATUS_UPDATED = "UPDATED"
STATUS_BROKEN  = "BROKEN"
STATUS_NEW     = "NEW"


def _load_todays_recs(user_id: str, window_str: str = "") -> list[dict]:
    """
    Load today's active recommendations from DB, matched by the exact
    window string (e.g. '7d') this scan was run with — a scan run with
    a DIFFERENT window earlier today should not be reloaded as if it
    were this scan's own morning picks.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT id, ticker, direction, strategy, expiry, dte,
                       conviction_score, thesis, webull_instructions,
                       legs, entry_debit, webull_limit_price, max_profit, max_loss,
                       risk_reward, invalidation_conditions, key_news,
                       signal_data, created_at, target_pct, stop_pct
                FROM daily_recommendations
                WHERE user_id=:uid AND date=CURRENT_DATE AND status='ACTIVE'
                  AND strategy != 'STOCK'
                  AND legs IS NOT NULL AND jsonb_array_length(legs) > 0
                  AND (:window_str = '' OR horizon = :window_str)
                ORDER BY conviction_score DESC
            """), {"uid": user_id, "window_str": window_str}).fetchall()

        picks = []
        for r in rows:
            sd = r.signal_data or {}
            picks.append({
                "id": str(r.id), "ticker": r.ticker, "direction": r.direction,
                "strategy": r.strategy or "", "expiry": str(r.expiry) if r.expiry else "",
                "dte": r.dte or 17, "conviction": r.conviction_score or 65,
                "confidence": r.conviction_score or 65, "thesis": r.thesis or "",
                "webull_instructions": r.webull_instructions or "", "legs": r.legs or [],
                "entry_debit": float(r.entry_debit or 0),
                "webull_limit_price": float(r.webull_limit_price or 0),
                "max_profit": float(r.max_profit or 0),
                "max_loss": float(r.max_loss or 0), "risk_reward": float(r.risk_reward or 0),
                "invalidation": r.invalidation_conditions or "", "key_news": r.key_news or "",
                "target_pct": float(r.target_pct) if r.target_pct is not None else None,
                "stop_pct": float(r.stop_pct) if r.stop_pct is not None else None,
                "market_view": sd.get("market_view", ""), "status": STATUS_INTACT,
                "status_reason": "", "scan_time": str(r.created_at)[:16] if r.created_at else "",
            })
        return picks
    except Exception as e:
        print(f"[Rescan] Could not load today's recs: {e}")
        return []


def _format_morning_pick(pick: dict) -> str:
    legs = pick.get("legs", [])
    strike_str = " / ".join(
        f"{l.get('action','')} ${l.get('strike',0):.0f}{l.get('type','')[0]}" for l in legs
    ) if legs else ""
    return (
        f"[{pick['ticker']}] {pick['direction']} {pick['strategy'].replace('_',' ')} "
        f"{strike_str} exp={pick['expiry']} conf={pick['conviction']}\n"
        f"  Thesis: {pick['thesis'][:120]}\n"
        f"  Breaks if: {pick['invalidation'][:80]}"
    )


def _build_validation_prompt(
    morning_picks: list[dict], fresh_candidates: list[dict], vix: dict,
    global_news: list[dict], budget: float, today: str,
    regime: dict | None = None,
    trading_window_days: int = 7,
    target_expiry: str = "",
) -> str:
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

    from app.recommendations.smart_engine import _compress_ticker
    candidates_block = "\n\n".join(_compress_ticker(c) for c in fresh_candidates[:10])
    news_str = "\n".join(f"  - {n.get('headline','')[:80]}" for n in (global_news or [])[:5])

    regime_str = ""
    if regime:
        ts  = regime.get("vix_structure", {})
        pcr = regime.get("put_call", {})
        regime_str = (
            f"\nVIX TERM STRUCTURE: VIX9D={ts.get('vix9d',0):.1f} vs VIX={ts.get('vix30',0):.1f} "
            f"({ts.get('signal','?')}) | PCR={pcr.get('pcr',0):.2f} ({pcr.get('signal','?')})"
            f"\nOVERALL BIAS: {regime.get('overall_bias','?')} — {regime.get('strategy_hint','')}"
        )

    return f"""You are managing real money. Today is {today}. Budget: ${budget:.2f}.

MARKET: VIX {vix.get('current', 17)} ({vix.get('zone','NORMAL')}) | {vix.get('trend','STABLE')}{regime_str}
NEWS: {news_str}

{morning_section}

=== FRESH CANDIDATES (new opportunities) ===
{candidates_block}

=== YOUR TASK ===
1. Validate each morning pick (INTACT/UPDATED/BROKEN)
2. REQUIRED: Always include SPY and QQQ in new_picks
   - Decide direction based on VIX trend + market flow
   - Pick best strategy: NAKED_CALL, NAKED_PUT, DEBIT_CALL_SPREAD, DEBIT_PUT_SPREAD, STRADDLE, STRANGLE, IRON_CONDOR
   - If VIX FALLING + bullish flow → CALL strategy; if VIX RISING → PUT or STRADDLE
3. Find additional NEW picks from fresh candidates
4. Return morning_status for existing picks + new_picks ranked by conviction

⚠️  USE EXACT PRICES SHOWN ABOVE — do NOT use your training data prices.

TARGET EXPIRY: user requested a {trading_window_days}-day trading window
(today + {trading_window_days} days, rolled to the next open trading day = {target_expiry}).
For each ticker, pick whichever expiry from that ticker's own listed
expiries (shown above) is CLOSEST to {target_expiry}. Real listed
expiries are fixed by the exchange — pick the nearest one available,
not an arbitrary date.

STRATEGY SELECTION (MANDATORY — pick based on conditions, and name which
one you used in "strategy_rule" — this is read downstream by the
paper-trade weekly review to find out which selection rule actually
correlates with wins, so it must be one of these exact tags):
  VIX_CONTANGO_NEUTRAL_IRON_CONDOR   — VIX STEEP_CONTANGO + NEUTRAL regime → IRON_CONDOR (sell both sides, collect premium)
  STRONG_FLOW_HIGH_CONVICTION_NAKED  — Strong directional flow + conviction >80 → NAKED_CALL or NAKED_PUT
  MODERATE_DIRECTIONAL_LOW_IV_DEBIT  — Moderate directional + IV rank <50 → DEBIT_CALL_SPREAD or DEBIT_PUT_SPREAD
  UNCLEAR_DIRECTION_BIG_MOVE_STRADDLE — Direction unclear + big move expected → STRADDLE or STRANGLE
  INDEX_NEUTRAL_IRON_CONDOR          — SPY/QQQ in NEUTRAL market → IRON_CONDOR is best fit

STRIKE RULES:
- Debit call spread: buy_strike LOWER than sell_strike
- Debit put spread: buy_strike HIGHER than sell_strike
- Naked call/put: buy_strike = ATM or slightly OTM, sell_strike = 0
- Iron condor: buy_strike = lower put, sell_strike = upper call (wider = safer)
- Max 8% OTM from current price

Respond ONLY with compact JSON — no prose, no markdown:
{{
  "market_view": "one sentence",
  "morning_status": {{
    "TICKER": {{"status": "INTACT", "reason": "5 words", "confidence": 75}}
  }},
  "new_picks": [
    {{"ticker": "SPY", "direction": "NEUTRAL", "strategy": "IRON_CONDOR",
     "expiry": "{target_expiry}", "buy_strike": 730.0, "sell_strike": 770.0,
     "reasoning": "brief", "key_risk": "brief", "confidence": 72,
     "strategy_rule": "INDEX_NEUTRAL_IRON_CONDOR"}},
    {{"ticker": "X", "direction": "BULLISH", "strategy": "NAKED_CALL",
     "expiry": "{target_expiry}", "buy_strike": 0.0, "sell_strike": 0.0,
     "reasoning": "brief", "key_risk": "brief", "confidence": 70,
     "strategy_rule": "STRONG_FLOW_HIGH_CONVICTION_NAKED"}}
  ]
}}"""


def rescan_with_validation(
    user_id: str,
    budget: float = 2000.0,
    pre_scanned: list | None = None,
    sector: str | None = None,
    cap_size: str | None = None,
    catalyst: str | None = None,
    trading_window_days: int = 7,
    stop_loss_pct: float = 40.0,
    profit_target_pct: float = 50.0,
) -> dict:
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
    from app.utils.scan_status import set_scan_status
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from app.recommendations.daily_engine import _check_api_health, _upsert_recommendation

    t_start = time.time()
    today   = datetime.now().strftime("%A %B %d, %Y")

    set_scan_status(user_id, "queued")

    # Pre-flight data-quality gate — skip a 60-80s scan (LLM call included)
    # against degraded upstream APIs rather than burning it on a low-quality
    # result. Ported from the old daily_engine.py scan path.
    api_health = _check_api_health()
    if api_health["data_quality"] < 0.5:
        return {
            "picks": [], "error": "insufficient_data",
            "message": f"API health too low ({api_health['data_quality']:.0%}) — retry at {api_health['retry_at']}",
            "api_health": api_health,
            "elapsed": round(time.time()-t_start, 1),
        }

    # Validate/normalize the user's real inputs — never silently
    # substitute a default the user didn't ask for.
    budget               = round_budget(budget)
    trading_window_days  = validate_trading_window(trading_window_days, "option")
    stop_loss_pct        = validate_pct(stop_loss_pct, "stop_loss_pct")
    profit_target_pct    = validate_pct(profit_target_pct, "profit_target_pct")

    target_expiry = compute_target_date(trading_window_days)
    window_str    = f"{trading_window_days}d"
    print(f"[Rescan] window={window_str} target_expiry={target_expiry} "
          f"stop={stop_loss_pct}% target={profit_target_pct}%")

    morning_picks = _load_todays_recs(user_id, window_str=window_str)
    print(f"[Rescan] {len(morning_picks)} morning picks loaded")
    set_scan_status(user_id, "prices")

    if sector and cap_size:
        from app.recommendations.filtered_universe import get_filtered_universe
        tickers = get_filtered_universe(sector, cap_size, catalyst or "any", user_id)
        print(f"[Rescan] Filtered universe: {len(tickers)} tickers ({sector}/{cap_size}/{catalyst})")
    elif pre_scanned:
        tickers = [p["ticker"] for p in pre_scanned]
    else:
        from app.scanner.universe import get_scan_universe
        tickers = get_scan_universe(user_id=user_id)

        # Don't recommend buying more of what's already held as a new
        # entry. Best-effort — a broker-fetch failure should never block
        # the scan itself.
        try:
            from app.broker.webull_connector import WebullConnector
            portfolio_syms = {p["symbol"] for p in WebullConnector(user_id).get_positions()}
            tickers_filtered = [t for t in tickers if t not in portfolio_syms]
            removed = len(tickers) - len(tickers_filtered)
            if removed > 0:
                print(f"[Rescan] Removed {removed} portfolio tickers from buy universe")
            tickers = tickers_filtered
        except Exception as e:
            print(f"[Rescan] Portfolio filter failed: {e} — using full universe")

    vix         = _build_vix_context()
    global_news = _build_global_news()
    regime      = get_full_market_regime()
    print(f"[Rescan] Market regime: {regime['overall_bias']} | {regime['summary']}")
    set_scan_status(user_id, "regime")

    earnings_pre  = get_earnings_premarket()  or []
    earnings_post = get_earnings_afterhours() or []
    earnings_map  = _build_earnings_map(earnings_pre, earnings_post)

    all_flow = get_flow_alerts(limit=500)        or []
    all_dp   = get_dark_pool_recent(limit=200)   or []
    flow_by  = {}
    for a in all_flow: flow_by.setdefault(a.get("ticker",""), []).append(a)
    dp_by = {}
    for d in all_dp:   dp_by.setdefault(d.get("ticker",""),  []).append(d)

    batch_flow = {t: compute_flow_score(alerts) for t, alerts in flow_by.items()}
    batch_dp   = {t: compute_dp_score(prints)   for t, prints in dp_by.items()}

    from datetime import datetime as _dt
    wl_sorted      = sorted(set(tickers))
    batch_idx      = (_dt.now().minute // 20) % 3
    rotation_slice = wl_sorted[batch_idx*43:(batch_idx+1)*43]
    uncovered      = [t for t in rotation_slice if t not in batch_flow and t not in batch_dp][:10]
    if uncovered:
        print(f"[Rescan] Per-ticker UW for {len(uncovered)} uncovered (batch {batch_idx}): {uncovered[:5]}...")

        def _tf(t):
            try:
                from app.options_flow.unusual_whales import get_flow_alerts as _fa, get_dark_pool_ticker as _dp
                return t, _fa(ticker=t, limit=20) or [], _dp(t, limit=20) or []
            except Exception:
                return t, [], []

        with ThreadPoolExecutor(max_workers=5) as ex:
            for fut in as_completed({ex.submit(_tf, t): t for t in uncovered}, timeout=30):
                try:
                    t, tf, td = fut.result()
                    if tf:
                        batch_flow[t] = compute_flow_score(tf)
                    if td:
                        batch_dp[t] = compute_dp_score(td)
                except Exception:
                    pass
        print(f"[Rescan] Total UW coverage: {len(batch_flow)} flow + {len(batch_dp)} dp tickers")

    if pre_scanned:
        picks = pre_scanned
    else:
        picks = quick_scan(tickers, user_id=user_id, top_n=15)

    import yfinance as _yf
    _spy_p = _yf.Ticker("SPY").fast_info.last_price or 0
    _qqq_p = _yf.Ticker("QQQ").fast_info.last_price or 0

    _spy_exp = nearest_friday_to(target_expiry)
    _qqq_exp = nearest_friday_to(target_expiry)

    index_candidates = [
        {"ticker": "SPY", "direction": "UNKNOWN", "price": _spy_p, "confidence": 75,
         "change_pct": 0, "flow_score": 0, "dp_score": 0, "sweeps": 0,
         "alert_count": 0, "score": 1.0, "signals": ["index"], "forced_expiry": _spy_exp},
        {"ticker": "QQQ", "direction": "UNKNOWN", "price": _qqq_p, "confidence": 75,
         "change_pct": 0, "flow_score": 0, "dp_score": 0, "sweeps": 0,
         "alert_count": 0, "score": 1.0, "signals": ["index"], "forced_expiry": _qqq_exp},
    ]
    print(f"[Rescan] SPY=${_spy_p:.2f} exp={_spy_exp} | QQQ=${_qqq_p:.2f} exp={_qqq_exp} "
          f"(window={trading_window_days}d, target={target_expiry})")
    set_scan_status(user_id, "enriching")

    for ip in index_candidates:
        ip["_locked_price"] = ip["price"]

    other_picks = [p for p in picks if p["ticker"] not in ("SPY","QQQ") and _is_optionable(p)]
    candidates = index_candidates + other_picks[:10]

    enriched = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(_enrich_ticker, c, earnings_map, batch_flow, batch_dp, user_id): c["ticker"]
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
    enriched_by_ticker = {c["ticker"]: c for c in enriched}
    set_scan_status(user_id, "llm_thinking")

    prompt = _build_validation_prompt(
        morning_picks, enriched, vix, global_news, budget, today,
        regime=regime, trading_window_days=trading_window_days,
        target_expiry=target_expiry,
    )
    llm_result = _call_smart_llm(prompt)
    set_scan_status(user_id, "llm_done")

    if not llm_result:
        option_picks = [p for p in morning_picks if p.get("legs") and len(p.get("legs",[])) > 0]
        print(f"[Rescan] LLM failed — returning {len(option_picks)} cached option picks (filtered from {len(morning_picks)})")
        return {
            "picks": option_picks, "market_view": "", "source": "morning_cache_fallback",
            "elapsed": round(time.time()-t_start, 1),
        }

    final = []
    morning_by_ticker = {p["ticker"]: p for p in morning_picks}
    print(f"[Rescan] morning_by_ticker: {list(morning_by_ticker.keys())}")

    morning_status = llm_result.get("morning_status", {})
    new_picks_llm  = llm_result.get("new_picks", [])
    market_view    = llm_result.get("market_view", "")

    if not morning_status and not new_picks_llm:
        new_picks_llm = llm_result.get("picks", [])
        new_picks_llm = [p for p in new_picks_llm if p.get("status") == STATUS_NEW
                         or p.get("status") not in (STATUS_INTACT, STATUS_BROKEN, STATUS_UPDATED)]

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

    for ticker, mp in morning_by_ticker.items():
        if ticker not in morning_status:
            mp = mp.copy()
            mp["status"]        = STATUS_INTACT
            mp["status_reason"] = "No change"
            final.append(mp)

    from app.recommendations.smart_engine import _execute_smart_rec
    for llm_pick in new_picks_llm:
        ticker = llm_pick.get("ticker", "")
        if not ticker or ticker in morning_by_ticker:
            continue

        rec = {
            "ticker": ticker, "direction": llm_pick.get("direction", "BULLISH"),
            "strategy": llm_pick.get("strategy", "DEBIT_CALL_SPREAD"),
            "expiry": llm_pick.get("expiry", ""), "dte": llm_pick.get("dte", trading_window_days),
            "buy_strike": llm_pick.get("buy_strike", 0), "sell_strike": llm_pick.get("sell_strike", 0),
            "reasoning": llm_pick.get("reasoning", ""), "key_risk": llm_pick.get("key_risk", ""),
            "catalyst": llm_pick.get("catalyst", ""), "confidence": llm_pick.get("confidence", 65),
        }

        trade = _execute_smart_rec(rec, budget, user_id)
        if trade:
            trade["status"]        = STATUS_NEW
            trade["status_reason"] = "Fresh pick"
            trade["confidence"]    = rec["confidence"]
            # These get written to the DB below via _upsert_recommendation —
            # also carry them on the returned object itself so the caller
            # (and this session's own verification) can actually see them,
            # instead of only existing silently inside the DB row.
            trade["target_pct"]    = profit_target_pct
            trade["stop_pct"]      = -stop_loss_pct
            # Which STRATEGY SELECTION rule the LLM says it followed (see
            # the prompt above) - read by the paper-trade-open job to find
            # out empirically which rule actually correlates with wins.
            trade["strategy_rule"] = llm_pick.get("strategy_rule", "")
            # Same signal snapshot already computed for the LLM prompt -
            # attach it to the pick itself so callers (the paper-trade-open
            # job specifically) don't need to re-fetch or re-derive it.
            enrich_ctx = enriched_by_ticker.get(ticker, {})
            trade["flow_score"] = enrich_ctx.get("flow_score", 0)
            trade["dp_score"]   = enrich_ctx.get("dp_score", 0)
            trade["oi_score"]   = enrich_ctx.get("oi_score", 0)
            trade["oi_max_days"] = enrich_ctx.get("oi_max_days", 0)
            trade["iv_current"] = enrich_ctx.get("iv_current", 0)
            final.append(trade)

            try:
                legs = trade.get("legs", [])
                entry_basis = abs(trade.get("entry_debit", 0))
                _upsert_recommendation(user_id, {
                    "ticker": ticker, "horizon": window_str,
                    "direction": trade.get("direction",""),
                    "conviction_score": trade.get("confidence",65),
                    "conviction_tier": "HIGH" if trade.get("confidence",0)>=75 else "MODERATE",
                    "act_now": trade.get("confidence",0)>=70, "position_size_guidance": "standard",
                    "thesis": rec.get("reasoning",""),
                    "entry_zone_low": entry_basis,
                    "entry_zone_high": entry_basis*1.05,
                    "entry_trigger": "AT_MARKET",
                    # Real user inputs — not a derived risk_reward figure,
                    # not a horizon-bucket default. These flow straight
                    # through to tracked_positions on confirm_execution.
                    # target_price/stop_price computed from entry_basis so
                    # format_daily_recommendations() shows real dollar
                    # figures instead of a hardcoded $0.
                    "target_price": round(entry_basis * (1 + profit_target_pct/100), 2),
                    "target_pct": profit_target_pct,
                    "stop_price": round(entry_basis * (1 - stop_loss_pct/100), 2),
                    "stop_pct": -stop_loss_pct,
                    "timeframe": f"{trading_window_days} days",
                    "invalidation_conditions": rec.get("key_risk",""),
                    "strategy": trade.get("strategy",""), "expiry": trade.get("expiry",""),
                    "dte": trade.get("dte",trading_window_days), "legs": legs,
                    "entry_debit": trade.get("entry_debit",0),
                    "webull_limit_price": trade.get("webull_limit_price", 0),
                    "total_cost": trade.get("total_cost",0),
                    "max_profit": trade.get("max_profit_per_contract",0),
                    "max_loss": trade.get("max_loss_per_contract",0),
                    "risk_reward": trade.get("risk_reward",0),
                    "webull_instructions": trade.get("webull_instructions",""),
                    "key_news": rec.get("catalyst","NONE"),
                    "warnings": trade.get("engine_warnings", []), "conviction_breakdown": {},
                    "signal_data": {"market_view": market_view},
                })
            except Exception as e:
                print(f"[Rescan] Store failed {ticker}: {e}")

    final = [p for p in final if p.get("legs") and len(p.get("legs", [])) > 0]

    status_order = {STATUS_UPDATED: 0, STATUS_NEW: 1, STATUS_INTACT: 2, STATUS_BROKEN: 99}
    final.sort(key=lambda x: (
        status_order.get(x.get("status", STATUS_INTACT), 2),
        -(x.get("confidence") or x.get("conviction",0))
    ))

    elapsed = round(time.time()-t_start, 1)
    print(f"[Rescan] COMPLETE in {elapsed}s — {len(final)} picks")

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
                        INSERT INTO signal_history (user_id,ticker,date,flow_score,dp_score,price,change_pct)
                        VALUES (:uid,:t,CURRENT_DATE,:fs,:dps,:p,:cp)
                        ON CONFLICT (user_id,ticker,date) DO UPDATE SET
                            flow_score=EXCLUDED.flow_score, dp_score=EXCLUDED.dp_score,
                            price=EXCLUDED.price, change_pct=EXCLUDED.change_pct
                    """),{"uid":user_id,"t":_tk,"fs":_p.get("flow_score",0),"dps":_p.get("dp_score",0),
                          "p":_p.get("price",0),"cp":_p.get("change_pct",0)})
                _saved += 1
            except Exception:
                pass
        print(f"[Rescan] Saved {_saved}/{len(_all_picks)} tickers to signal_history")
    except Exception:
        pass

    set_scan_status(user_id, "complete")

    return {
        "picks": final, "market_view": llm_result.get("market_view",""),
        "source": "rescan_validated", "elapsed": elapsed, "vix": vix.get("current"), "date": today,
        "trading_window_days": trading_window_days, "target_expiry": target_expiry,
        "stop_loss_pct": stop_loss_pct, "profit_target_pct": profit_target_pct,
    }
