"""
Bull Agent / Bear Agent (MULTIAGENT_MIGRATION.md items 11-13).

Today's smart_engine.py makes ONE batched LLM call that picks BOTH
direction and strategy across up to ~10 candidates at once. These are
the opposite: one call per agent per ticker, direction already decided
by Prediction Agent's routing pass (app/agents/prediction_agent.py) -
each agent's only job is building the strongest HONEST case for its
assigned side, refining the rough strategy_shape/rough_strikes routing
already computed rather than re-deciding direction from scratch.

Two invocation modes (item 13), dispatched by run_bull_bear_debate():
  "routed":     one agent only - whichever side Prediction Agent
                actually routed to (BULL_ONLY -> Bull, BEAR_ONLY ->
                Bear) - the main pass.
  "tie_break":  both agents, same ticker, run only after the main pass
                fully completes (MULTIAGENT_MIGRATION.md item 21 - a
                direct, deliberate consequence of the enrichment-
                timeout/rate-limiter bug found earlier this session:
                concurrent threads competing for the same rate-limited
                UW resources). Each agent argues its side independently
                and is explicitly told to report low confidence rather
                than manufacture a case if the real data doesn't
                support it - the later tie-break resolution (items
                18-19's Finalize Phase, not built here) needs an honest
                signal to compare, not persuasive writing.

Output JSON shape matches the existing single-candidate contract
smart_engine.py::_execute_smart_rec already consumes (ticker,
direction, strategy, expiry, dte, buy_strike, sell_strike, reasoning,
key_risk, confidence, catalyst) - deliberate, so Bull/Bear output can
eventually feed the same deterministic trade-math path without a new
contract, though wiring that all the way through isn't done here.
"""
import json


def _compress_candidate(t: dict) -> str:
    """Same per-ticker signal summary smart_engine.py::_compress_ticker
    already uses, kept local here rather than imported - this module
    should not depend on smart_engine's private helpers."""
    expiry_str = " | ".join(
        f"{e['expiry']}({e['dte']}d,IV{e['iv_pct']:.0f}%)" for e in t.get("expiries", [])[:5]
    ) or "no expiries available"
    news_str = " // ".join(t.get("news", [])[:2]) or "no news"
    earn = t.get("earnings_days", 999)
    earn_str = (f"EARNINGS {earn}d (move±{t.get('expected_move', 0):.1f}%)"
                if earn < 60 else "no near earnings")

    if t.get("iv_exp_signal") == "INSUFFICIENT_HISTORY":
        iv_exp_str = f"insufficient_history({t.get('iv_exp_days', 0)}d)"
    else:
        iv_exp_str = f"{t.get('iv_exp_score', 0):+.0f}({t.get('iv_exp_signal', '')})"

    return (
        f"[{t.get('ticker','?')} | ${t.get('price',0):.2f} | "
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
        f" IVexp:{iv_exp_str}"
        f" Congress:{t.get('congress_text','none')} InstOwn:{t.get('inst_own_score',50):.0f}/100"
    )


def _build_agent_prompt(
    side: str, enriched: dict, routing: dict, market_context: dict | None,
    retrieval_context: str, today_str: str,
) -> str:
    ticker = enriched.get("ticker", routing.get("ticker", "?"))
    opposite = "BEARISH" if side == "BULLISH" else "BULLISH"

    strikes_str = ", ".join(
        f"{leg['action']} {leg['type']} {leg['strike']}" for leg in routing.get("rough_strikes", [])
    ) or "none computed"

    mc = market_context or {}
    vix_line   = f"VIX: {mc.get('vix', {}).get('current', '?')} ({mc.get('vix', {}).get('zone', '?')})"
    regime     = mc.get("regime", {})
    regime_line = f"Regime: {regime.get('overall_bias', '?')} — {regime.get('strategy_hint', '')}"
    econ_line  = f"Macro events: {mc.get('econ_events', 'no data')}"

    retrieval_block = (
        f"\n=== SIMILAR PAST OUTCOMES ===\n{retrieval_context}\n"
        if retrieval_context else ""
    )

    return f"""You are a {side} options analyst. Today is {today_str}.
Your ONLY job: build the STRONGEST HONEST {side.lower()} case for {ticker}.
Direction is NOT yours to decide — Prediction Agent already routed this
candidate as {side}. Do not argue for {opposite}. Do not change direction.

If the real signals below genuinely do NOT support a strong {side.lower()}
case, say so — report a LOW confidence (below 50), don't manufacture
conviction. An honest low-confidence {side.lower()} case is more useful
than a persuasive but unsupported one, especially for tie-break
resolution downstream.

=== PREDICTION AGENT'S ROUTING (starting point, refine don't reinvent) ===
Rough strategy shape: {routing.get('strategy_shape', '?')}
Rough strikes: {strikes_str}
Rough expiry: {routing.get('expiry', '?')}
Deterministic routing confidence: {routing.get('confidence_of_routing', 0)}/100
Rough R/R gate: {'CLEARS' if routing.get('clears_rough_rr_gate') else 'DOES NOT CLEAR'} ({routing.get('rough_rr', 0):.2f})
Signals that drove this routing: {', '.join(routing.get('signal_reasons', [])) or 'none'}

=== CANDIDATE SIGNALS ===
{_compress_candidate(enriched)}

=== MARKET CONTEXT ===
{vix_line}
{regime_line}
{econ_line}
{retrieval_block}
=== YOUR TASK ===
Refine (don't reinvent from scratch) the rough strategy/strikes above
into a real trade recommendation, using real listed expiries from the
candidate signals. Pick the expiry/strikes that maximize probability of
profit for a {side.lower()} thesis specifically.

STRATEGIES AVAILABLE: NAKED_CALL, NAKED_PUT, DEBIT_CALL_SPREAD,
DEBIT_PUT_SPREAD, CREDIT_CALL_SPREAD, CREDIT_PUT_SPREAD, IRON_CONDOR,
STRADDLE, STRANGLE — stay consistent with a {side.lower()} thesis
(e.g. IRON_CONDOR/STRADDLE only make sense if you're arguing this is a
lower-conviction/range-bound flavor of {side.lower()}, not a real
direction change).

Respond with valid JSON only:
{{
  "ticker": "{ticker}", "direction": "{side}", "strategy": "...",
  "expiry": "YYYY-MM-DD", "dte": 21, "buy_strike": 0.0, "sell_strike": 0.0,
  "reasoning": "2 sentences: the strongest honest {side.lower()} case",
  "key_risk": "1 sentence — what would invalidate this {side.lower()} thesis",
  "confidence": 50, "catalyst": "what would move it {side.lower()}"
}}"""


def _call_agent_llm(prompt: str, agent_name: str) -> dict | None:
    from app.utils.config import settings
    import requests as req
    import re

    system = (
        f"You are an expert options analyst arguing one side of a debate. "
        f"Respond with valid JSON only — no text before or after."
    )
    try:
        payload = {
            "model": settings.ollama_model, "prompt": prompt, "system": system, "stream": False,
            "options": {"num_predict": 2000, "temperature": 0.05, "top_p": 0.9, "num_ctx": 8192},
        }
        r   = req.post(f"{settings.ollama_host}/api/generate", json=payload, timeout=90)
        raw = r.json().get("response", "").strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            if "direction" in data and "confidence" in data:
                return data
        print(f"[{agent_name}] Could not parse: {raw[:300]}")
        return None
    except Exception as e:
        print(f"[{agent_name}] Error: {e}")
        return None


def run_bull_agent(
    enriched: dict, routing: dict, market_context: dict | None = None,
    mode: str = "routed", retrieval_context: str = "",
) -> dict | None:
    from datetime import datetime
    today = datetime.now().strftime("%A %B %d, %Y")
    prompt = _build_agent_prompt("BULLISH", enriched, routing, market_context, retrieval_context, today)
    result = _call_agent_llm(prompt, "BullAgent")
    if result:
        result["agent"] = "bull"
        result["mode"]  = mode
    return result


def run_bear_agent(
    enriched: dict, routing: dict, market_context: dict | None = None,
    mode: str = "routed", retrieval_context: str = "",
) -> dict | None:
    from datetime import datetime
    today = datetime.now().strftime("%A %B %d, %Y")
    prompt = _build_agent_prompt("BEARISH", enriched, routing, market_context, retrieval_context, today)
    result = _call_agent_llm(prompt, "BearAgent")
    if result:
        result["agent"] = "bear"
        result["mode"]  = mode
    return result


def run_bull_bear_debate(
    enriched: dict, routing: dict, market_context: dict | None = None,
    retrieval_context: str = "",
) -> dict:
    """
    Item 13 dispatcher: reads routing["routing_classification"]
    (from prediction_agent.py::classify_and_store) and calls the
    right agent(s) - one for BULL_ONLY/BEAR_ONLY (routed mode), both
    for TIE_BREAK (tie_break mode).
    """
    classification = routing.get("routing_classification")
    ticker = enriched.get("ticker", routing.get("ticker", "?"))

    if classification == "BULL_ONLY":
        bull = run_bull_agent(enriched, routing, market_context, mode="routed", retrieval_context=retrieval_context)
        return {"ticker": ticker, "mode": "routed", "bull": bull, "bear": None}

    if classification == "BEAR_ONLY":
        bear = run_bear_agent(enriched, routing, market_context, mode="routed", retrieval_context=retrieval_context)
        return {"ticker": ticker, "mode": "routed", "bull": None, "bear": bear}

    if classification == "TIE_BREAK":
        bull = run_bull_agent(enriched, routing, market_context, mode="tie_break", retrieval_context=retrieval_context)
        bear = run_bear_agent(enriched, routing, market_context, mode="tie_break", retrieval_context=retrieval_context)
        return {"ticker": ticker, "mode": "tie_break", "bull": bull, "bear": bear}

    return {"ticker": ticker, "mode": None, "bull": None, "bear": None,
            "error": f"unknown routing_classification: {classification!r}"}
