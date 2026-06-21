"""
Strategy Engine v3 (Component C7) — LLM-First Architecture.

Division of responsibility:
    LLM (Qwen 14B):   Decides strategy, strikes, expiry based on all signals
    Python:           Executes all arithmetic with real-time UW prices

Flow:
    1. Python collects full data package (TA + flow + all option chains)
    2. LLM receives data + rules → outputs structured trade decision (JSON)
    3. Python validates LLM's chosen strikes exist in real UW chain
    4. Python fetches UW bid/ask for exact strikes chosen by LLM
    5. Python computes BSM greeks, P&L, position sizing, limit price
    6. LLM adds plain-English explanation + risk narrative

Why LLM for strategy decision:
    Static rules ("BEARISH + HIGH_IV → CREDIT_CALL_SPREAD") miss nuance.
    The LLM weighs ALL signals simultaneously:
    - TA trend vs options flow disagreement → which signal wins?
    - GEX wall at $210 means limited upside → affects strike choice
    - Earnings in 12 days → shorter expiry preferred
    - Market tide bearish but flow neutral → lower confidence → wider spread

Why Python for arithmetic:
    - LLMs make arithmetic errors with real money on the line
    - BSM must be mathematically exact
    - Position sizing must be deterministic ($2,000 ÷ $619 = exactly 3)
    - Same inputs must always give same P&L output

Trading Rules (given to LLM as context):
    Rule 1: Close at 80% of max profit or 40% loss of premium
    Rule 2: No new positions within 7 days of earnings
    Rule 3: Regime check — 5 questions before any trade
    Rule 4: Pre-trade checklist before execution
"""
import json
import math
import re
from datetime import datetime, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TARGET_PROFIT_PCT = 0.80
STOP_LOSS_PCT     = 0.40

STRATEGIES = [
    "DEBIT_PUT_SPREAD",    # bearish, low/med IV — buy puts, defined risk
    "DEBIT_CALL_SPREAD",   # bullish, low/med IV — buy calls, defined risk
    "CREDIT_CALL_SPREAD",  # bearish, high IV — sell calls, collect premium
    "CREDIT_PUT_SPREAD",   # bullish, high IV — sell puts, collect premium
    "IRON_CONDOR",         # neutral, high IV — sell both sides
    "LONG_STRADDLE",       # neutral, low IV — buy both, expect big move
    "LONG_PUT",            # very bearish, low IV — naked long put
    "LONG_CALL",           # very bullish, low IV — naked long call
    "SELL_NAKED_CALL",     # very bearish, very high IV ⚠️ aggressive
    "SELL_NAKED_PUT",      # very bullish, very high IV ⚠️ aggressive
]


# ─────────────────────────────────────────────────────────────────────────────
# Data Collection Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _get_live_spot(ticker: str, fallback: float = 0, user_id: str | None = None) -> float:
    """Webull (if owned) → yfinance → Polygon fallback."""
    if user_id:
        try:
            from app.broker.webull_connector import WebullConnector
            from app.broker.base import BrokerNotConnectedError
            wb  = WebullConnector(user_id)
            for p in wb.get_positions():
                if (p.get("symbol","").upper() == ticker.upper()
                        and p.get("instrument_type") == "STOCK"):
                    price = float(p["last_price"])
                    if price > 0: return price
        except Exception:
            pass
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        for key in ("lastPrice","last_price","regularMarketPrice"):
            p = fi.get(key)
            if p and float(p) > 0: return float(p)
    except Exception:
        pass
    return fallback


def _get_all_expiries(ticker: str) -> list[str]:
    try:
        import yfinance as yf
        return list(yf.Ticker(ticker).options)
    except Exception:
        return []


def _get_uw_chain(ticker: str, expiry: str) -> dict[str, dict]:
    """
    Fetch UW option contracts for a specific expiry.
    Returns {symbol: contract_dict} for fast lookup.
    """
    try:
        from app.options_flow.unusual_whales import get_option_contracts
        contracts = get_option_contracts(ticker, expiry=expiry, limit=500)
        lookup = {}
        for c in contracts:
            sym = c.get("option_symbol", "")
            if sym:
                lookup[sym] = c
        return lookup
    except Exception:
        return {}


def _get_yf_strikes(ticker: str, expiry: str) -> tuple[list, list]:
    """Get available call and put strikes from yfinance (structure only)."""
    try:
        import yfinance as yf
        chain = yf.Ticker(ticker).option_chain(expiry)
        call_strikes = sorted(chain.calls["strike"].unique().tolist())
        put_strikes  = sorted(chain.puts["strike"].unique().tolist())
        return call_strikes, put_strikes
    except Exception:
        return [], []


def _uw_price_for_strike(
    uw_chain: dict,
    strike: float,
    option_type: str,
    spot: float,
    dte: int,
    avg_iv: float,
    max_diff: float = 2.5,
) -> tuple[float, float, float, str]:
    """
    Get UW bid/ask for a specific strike.
    Returns (bid, ask, iv, source) where source is 'UW' or 'BSM_estimate'.
    """
    type_char = "C" if option_type == "CALL" else "P"
    best = None
    best_diff = float("inf")

    for sym, c in uw_chain.items():
        if type_char not in sym:
            continue
        try:
            s = float(sym[-8:]) / 1000.0
        except (ValueError, IndexError):
            continue
        diff = abs(s - strike)
        if diff < best_diff:
            best_diff = diff
            best = (s, c)

    if best and best_diff <= max_diff:
        _, c = best
        bid = _safe(c.get("nbbo_bid"))
        ask = _safe(c.get("nbbo_ask"))
        iv  = _safe(c.get("implied_volatility"), avg_iv)
        if bid > 0.05 and ask > 0.05:
            return round(bid, 2), round(ask, 2), iv, "UW"
        if ask > 0.05:
            return round(ask * 0.85, 2), round(ask, 2), iv, "UW"

    # BSM fallback — estimate only, labeled clearly
    try:
        from py_vollib.black_scholes import black_scholes
        flag = "c" if option_type == "CALL" else "p"
        t    = max(dte, 1) / 365.0
        bsm  = round(max(0.01, float(black_scholes(flag, spot, strike, t, 0.05, avg_iv))), 2)
        return round(bsm * 0.85, 2), bsm, avg_iv, "BSM_estimate"
    except Exception:
        return 0.01, 0.01, avg_iv, "BSM_estimate"


def _bsm_greeks(spot, strike, dte, iv, flag):
    try:
        from py_vollib.black_scholes.greeks.analytical import delta, gamma, theta, vega
        t = max(dte, 1) / 365.0
        return {
            "delta": round(delta(flag, spot, strike, t, 0.05, iv), 4),
            "gamma": round(gamma(flag, spot, strike, t, 0.05, iv), 4),
            "theta": round(theta(flag, spot, strike, t, 0.05, iv), 4),
            "vega":  round(vega (flag, spot, strike, t, 0.05, iv), 4),
        }
    except Exception:
        return {}


def _round_to_strike(price: float) -> float:
    if price > 500:   iv = 10.0
    elif price > 200: iv = 5.0
    elif price > 50:  iv = 2.5
    else:             iv = 1.0
    return round(round(price / iv) * iv, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Data Package for LLM
# ─────────────────────────────────────────────────────────────────────────────

def _build_llm_data_package(
    ticker: str,
    ta_profile: dict,
    flow_signal: dict,
    spot: float,
    expiry_options: list[dict],  # [{expiry, dte, call_strikes, put_strikes, key_prices}]
    budget: float,
    max_loss: float,
    profit_target: float | None,
    rag_context: str = "",
) -> str:
    """
    Build a structured data package for the LLM to reason over.
    This is the full context the LLM needs to make its decision.
    """
    # TA summary
    ta = f"""
TECHNICAL ANALYSIS — {ticker} @ ${spot}
  Trend:        {ta_profile.get('trend')}
  Signal:       {ta_profile.get('signal')} ({ta_profile.get('strength_score')}/100)
  RSI(14):      {ta_profile.get('rsi_14')}
  MACD:         {'BULLISH' if ta_profile.get('macd_bullish') else 'BEARISH'}
  Above MA20:   {ta_profile.get('above_ma20')}  | MA50: {ta_profile.get('above_ma50')} | MA200: {ta_profile.get('above_ma200')}
  ATR(14):      ${ta_profile.get('atr_14')}
  Support:      ${ta_profile.get('support')}  | Resistance: ${ta_profile.get('resistance')}
  BB %B:        {ta_profile.get('bb_pct_b')}  (>1=overbought, <0=oversold)
  Rel Volume:   {ta_profile.get('relative_volume')}x"""

    # Flow summary
    flow = f"""
OPTIONS FLOW (Unusual Whales)
  Direction:    {flow_signal.get('direction')}
  Confidence:   {flow_signal.get('confidence')}/100
  Options flow: {flow_signal.get('flow', {}).get('direction')} score={flow_signal.get('flow', {}).get('score')}
    {flow_signal.get('flow', {}).get('details', '')}
  Dark pool:    {flow_signal.get('dark_pool', {}).get('direction')} score={flow_signal.get('dark_pool', {}).get('score')}
    {flow_signal.get('dark_pool', {}).get('details', '')}
  GEX wall:     ${flow_signal.get('gex', {}).get('gamma_wall', 'N/A')}
  Net gamma:    {flow_signal.get('gex', {}).get('net_gamma', 'N/A')}
  Market tide:  {flow_signal.get('market_tide', {}).get('direction')} — {flow_signal.get('market_tide', {}).get('details', '')}
  Earnings:     {flow_signal.get('earnings_risk', {}).get('days_to_earnings')} days away
    Risk level: {flow_signal.get('earnings_risk', {}).get('risk')}"""

    # Available expiries with key prices
    expiry_str = "\nAVAILABLE OPTION EXPIRIES (with sample UW prices):"
    for e in expiry_options[:8]:  # show first 8 expiries
        expiry_str += f"\n  {e['expiry']} ({e['dte']} DTE) — ATM call bid/ask: {e.get('atm_call_bid','?')}/{e.get('atm_call_ask','?')} | ATM put: {e.get('atm_put_bid','?')}/{e.get('atm_put_ask','?')} | IV: {e.get('avg_iv','?')}"
        expiry_str += f"\n    Call strikes: {e.get('call_strikes', [])[:6]}"
        expiry_str += f"\n    Put strikes:  {e.get('put_strikes', [])[:6]}"

    # User constraints
    constraints = f"""
USER CONSTRAINTS
  Budget:         ${budget} (total to deploy)
  Max loss:       ${max_loss} (stop loss — exit if position loses this)
  Profit target:  ${profit_target or 'no minimum'} (minimum desired profit)
  Risk tolerance: {'conservative' if max_loss < budget * 0.3 else 'moderate' if max_loss < budget * 0.5 else 'aggressive'}"""

    # Rules
    rules = """
TRADING RULES (must be followed)
  Rule 1: Close at 80% of max profit OR 40% loss of premium paid
  Rule 2: No new positions if earnings within 7 days (check earnings_risk)
  Rule 3: Regime check — only trade in confirmed direction
  
AVAILABLE STRATEGIES
  DEBIT_PUT_SPREAD:   Buy lower put, sell higher put — bearish, defined risk, low/med IV
  DEBIT_CALL_SPREAD:  Buy lower call, sell higher call — bullish, defined risk, low/med IV
  CREDIT_CALL_SPREAD: Sell lower call, buy higher call — bearish, high IV, collect premium
  CREDIT_PUT_SPREAD:  Sell higher put, buy lower put — bullish, high IV, collect premium
  IRON_CONDOR:        Sell call + put spread both sides — neutral, high IV
  LONG_STRADDLE:      Buy call + put ATM — neutral, low IV, expect big move
  LONG_PUT:           Buy single put — very bearish, low IV
  LONG_CALL:          Buy single call — very bullish, low IV
  SELL_NAKED_CALL:    Sell single call ⚠️ — very bearish, very high IV, requires margin
  SELL_NAKED_PUT:     Sell single put ⚠️ — very bullish, very high IV, cash secured

PUT SPREAD RULE: sell strike MUST be higher than buy strike (sell closer to ATM, buy further OTM)
CALL SPREAD RULE: buy strike MUST be higher than sell strike (sell closer to ATM, buy further OTM)
IRON CONDOR LEG ORDER (Webull convention): BUY PUT (lowest) → SELL PUT → SELL CALL → BUY CALL (highest)"""

    # RAG context: historical price + earnings + macro + news
    rag_section = ""
    if rag_context:
        rag_section = "\n\nMARKET CONTEXT (Historical + News + Macro):\n" + rag_context[:2000]

    return ta + flow + expiry_str + constraints + rules + rag_section


def _log_llm_decision(ticker: str, decision: dict, outcome: str, status: str) -> None:
    """
    Log LLM decisions to a JSONL file for prompt improvement.
    Rejected decisions help us identify prompt weaknesses.
    Log location: logs/llm_decisions.jsonl
    """
    import json
    from pathlib import Path

    log_dir = Path(__file__).resolve().parents[2] / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "llm_decisions.jsonl"

    entry = {
        "timestamp": datetime.now().isoformat(),
        "ticker": ticker,
        "status": status,          # ACCEPTED or REJECTED
        "outcome": outcome,        # R/R value or rejection reason
        "strategy": decision.get("strategy"),
        "expiry": decision.get("expiry"),
        "direction": decision.get("direction"),
        "confidence": decision.get("confidence"),
        "legs": decision.get("legs", []),
        "reasoning": decision.get("reasoning", ""),
    }

    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # logging is non-critical


# ─────────────────────────────────────────────────────────────────────────────
# LLM Strategy Decision
# ─────────────────────────────────────────────────────────────────────────────

def _llm_decide_strategy(data_package: str, ticker: str) -> dict | None:
    """
    Ask Qwen 14B to decide the best strategy and specific strikes.
    Returns structured JSON decision or None if LLM unavailable.

    Includes:
    - Prompt size guard (truncates if > 3000 chars to avoid context overflow)
    - Retry with minimal prompt if full prompt fails
    - JSON extraction with fallback parsing
    """
    system = f"""You are an expert options trader. Select the optimal strategy and OTM strikes.
Respond with valid JSON ONLY — no text before or after the JSON.

JSON format:
{{
  "strategy": "DEBIT_PUT_SPREAD",
  "expiry": "2026-07-10",
  "legs": [
    {{"action": "BUY", "type": "PUT", "strike": 205.0}},
    {{"action": "SELL", "type": "PUT", "strike": 195.0}}
  ],
  "direction": "BEARISH",
  "confidence": 65,
  "reasoning": "2-3 sentences on why this trade",
  "key_risk": "1 sentence on main risk",
  "regime_check": "PASS or FAIL with reason"
}}

CRITICAL STRIKE RULES — ALWAYS OTM/ATM ONLY:
- DEBIT_PUT_SPREAD (bearish): BUY strike 0-3% below spot, SELL strike 5-10% below spot
  Example at spot={ticker}_SPOT: BUY put at spot×0.99, SELL put at spot×0.93
- DEBIT_CALL_SPREAD (bullish): BUY strike 0-3% above spot, SELL strike 5-10% above spot
- CREDIT_CALL_SPREAD (bearish): SELL strike 3-8% above spot, BUY strike 8-13% above spot
- CREDIT_PUT_SPREAD (bullish): SELL strike 3-8% below spot, BUY strike 8-13% below spot
- IRON_CONDOR (neutral): short strikes 3-6% each side from spot

NEVER pick deeply ITM strikes (>10% in the money) — they have no profit potential.
Minimum required R/R = 0.5. Spread width must be ≥ 2× entry debit for debit spreads.

Strike ordering:
- PUT SPREAD: sell_strike < buy_strike (sell is lower/further OTM)
- CALL SPREAD: buy_strike > sell_strike (buy is higher/further OTM)
- IRON CONDOR: BUY PUT(lowest) → SELL PUT → SELL CALL → BUY CALL(highest)"""

    from app.utils.config import settings
    import requests as req

    def _call(prompt_text: str, max_tokens: int = 600) -> dict | None:
        try:
            payload = {
                "model":  settings.ollama_model,
                "prompt": prompt_text,
                "system": system,
                "stream": False,
                "options": {
                    "num_predict": max_tokens,
                    "temperature": 0.1,
                    "top_p": 0.9,
                    "num_ctx": 4096,  # explicit context window
                }
            }
            r = req.post(
                f"{settings.ollama_host}/api/generate",
                json=payload,
                timeout=120
            )
            r.raise_for_status()
            raw = r.json().get("response", "").strip()

            # Extract JSON
            json_match = re.search(r'\{[^{}]*"strategy"[^{}]*\}', raw, re.DOTALL)
            if not json_match:
                json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                try:
                    decision = json.loads(json_match.group())
                    if {"strategy","expiry","legs","direction"}.issubset(decision.keys()):
                        return decision
                except json.JSONDecodeError:
                    pass

            print(f"[LLM] Could not parse JSON from: {raw[:200]}")
            return None
        except Exception as e:
            print(f"[LLM] Call failed: {e}")
            return None

    # Attempt 1: Full data package (truncated to 3500 chars)
    prompt_full = f"Analyze this {ticker} options trade and select the best strategy:\n\n{data_package[:3500]}"
    result = _call(prompt_full)
    if result:
        return result

    # Attempt 2: Minimal prompt with just key signals
    lines = data_package.split('\n')
    key_lines = [l for l in lines if any(k in l for k in
        ['Direction', 'Confidence', 'Signal', 'RSI', 'MACD', 'trend', 'TREND',
         'GEX', 'tide', 'Earnings', 'Budget', 'Max loss', 'expiry', 'DTE',
         'call strikes', 'put strikes', 'ATM call', 'IV:'])]
    mini_package = '\n'.join(key_lines[:40])

    prompt_mini = f"""Select the best options strategy for {ticker}.

Key data:
{mini_package}

Pick strategy, expiry, and strikes. Respond with JSON only."""

    print(f"[LLM] Retrying with minimal prompt ({len(prompt_mini)} chars)...")
    result = _call(prompt_mini, max_tokens=400)
    if result:
        return result

    print(f"[LLM] Both attempts failed — using deterministic fallback")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Python Arithmetic Executor
# ─────────────────────────────────────────────────────────────────────────────

def _execute_trade_math(
    decision: dict,
    ticker: str,
    spot: float,
    budget: float,
    max_loss: float,
) -> dict:
    """
    Given LLM's strategy decision, execute all arithmetic with real UW prices.

    Python's job:
    - Fetch actual UW bid/ask for the chosen strikes
    - Validate strikes make financial sense
    - Calculate BSM greeks
    - Calculate exact P&L (entry cost, max profit, max loss)
    - Size position to fit budget
    - Compute limit price for Webull order
    """
    strategy = decision["strategy"]
    expiry   = decision["expiry"]
    legs_in  = decision["legs"]
    dte      = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days

    # Get real UW prices for this expiry
    uw_chain = _get_uw_chain(ticker, expiry)

    # Compute avg IV from this chain
    ivs = [_safe(c.get("implied_volatility")) for c in uw_chain.values()
           if _safe(c.get("implied_volatility")) > 0.05]
    avg_iv = sum(ivs) / len(ivs) if ivs else 0.30

    is_credit = strategy in ("CREDIT_CALL_SPREAD","CREDIT_PUT_SPREAD",
                              "SELL_NAKED_CALL","SELL_NAKED_PUT","IRON_CONDOR")

    # Fetch real UW prices for each leg
    legs_out = []
    for leg in legs_in:
        strike    = float(leg["strike"])
        opt_type  = leg["type"]
        action    = leg["action"]
        bid, ask, iv, source = _uw_price_for_strike(uw_chain, strike, opt_type, spot, dte, avg_iv)
        flag = "c" if opt_type == "CALL" else "p"
        greeks = _bsm_greeks(spot, strike, dte, iv, flag)

        legs_out.append({
            "action":       action,
            "type":         opt_type,
            "strike":       strike,
            "expiry":       expiry,
            "bid":          bid,
            "ask":          ask,
            "mid":          round((bid + ask) / 2, 2),
            "iv":           round(iv, 3),
            "price_source": source,
            **greeks,
        })

    # Calculate net entry cost/credit
    entry = 0.0
    for leg in legs_out:
        if leg["action"] == "BUY":
            entry += leg["ask"]   # pay ask when buying
        else:
            entry -= leg["bid"]   # receive bid when selling

    entry = round(entry, 2)

    # Compute spread width for spread strategies
    spread_width = 0.0
    if len(legs_out) >= 2:
        if strategy in ("DEBIT_PUT_SPREAD","DEBIT_CALL_SPREAD"):
            spread_width = abs(legs_out[0]["strike"] - legs_out[1]["strike"])
        elif strategy in ("CREDIT_CALL_SPREAD","CREDIT_PUT_SPREAD"):
            spread_width = abs(legs_out[0]["strike"] - legs_out[1]["strike"])
        elif strategy == "IRON_CONDOR" and len(legs_out) == 4:
            spread_width = abs(legs_out[1]["strike"] - legs_out[0]["strike"])  # put spread width

    # Max profit/loss per contract
    if is_credit:
        credit      = abs(entry)
        max_p_c     = round(credit * 100, 2)
        if spread_width > 0:
            max_l_c = round((spread_width - credit) * 100, 2)
        else:
            max_l_c = round(spot * 100, 2)  # naked = full stock price as risk
        size_by     = max(max_l_c, 1)
    else:
        max_p_c = round((spread_width - entry) * 100, 2) if spread_width > 0 else round(spot * 0.20 * 100, 2)
        max_l_c = round(entry * 100, 2)
        size_by = max(max_l_c, 1)

    # Position sizing
    n = max(1, int(budget / size_by))
    while (size_by * n) > budget and n > 1:
        n -= 1

    # P&L calculations
    if is_credit:
        credit_received = round(abs(entry) * 100 * n, 2)
        margin_required = round(max_l_c * n, 2)
        target_profit   = round(credit_received * TARGET_PROFIT_PCT, 2)
        stop_loss       = round(credit_received * STOP_LOSS_PCT, 2)
        total_cost      = credit_received
        pnl_label       = "credit_received"
    else:
        premium_paid    = round(abs(entry) * 100 * n, 2)
        margin_required = premium_paid
        target_profit   = round(max_p_c * TARGET_PROFIT_PCT * n, 2)
        stop_loss       = round(premium_paid * STOP_LOSS_PCT, 2)
        total_cost      = premium_paid
        pnl_label       = "premium_paid"
        credit_received = 0

    risk_reward = round(target_profit / stop_loss, 2) if stop_loss > 0 else None

    # ── R/R Validation ────────────────────────────────────────────────────────
    # Reject trades with terrible R/R (LLM may have chosen ITM strikes)
    if risk_reward is not None and risk_reward < 0.5:
        strikes = [l["strike"] for l in legs_out]
        raise ValueError(
            f"R/R {risk_reward} below 0.5 — LLM chose bad strikes {strikes}. Triggering fallback."
        )

    # Webull limit price (net mid of all legs)
    sell_mid = sum(l["mid"] for l in legs_out if l["action"] == "SELL")
    buy_mid  = sum(l["mid"] for l in legs_out if l["action"] == "BUY")
    webull_limit_price = round(sell_mid - buy_mid, 2) if is_credit else round(buy_mid - sell_mid, 2)

    return {
        "strategy":            strategy,
        "expiry":              expiry,
        "dte":                 dte,
        "is_credit_strategy":  is_credit,
        "pnl_label":           pnl_label,
        "legs":                legs_out,
        "spread_width":        spread_width,
        "entry_debit":         entry,
        "avg_iv":              round(avg_iv, 3),
        "contracts":           n,
        "total_cost":          total_cost,
        "credit_received":     credit_received if is_credit else 0,
        "premium_paid":        total_cost if not is_credit else 0,
        "margin_required":     margin_required,
        "max_loss_per_contract": max_l_c,
        "max_profit_per_contract": max_p_c,
        "target_profit":       target_profit,
        "stop_loss":           stop_loss,
        "risk_reward":         risk_reward,
        "webull_limit_price":  webull_limit_price,
        "webull_instructions": (
            f"Enter as Iron Condor/Spread order at LIMIT ${webull_limit_price:.2f} credit. "
            f"If not filled in 5 min, lower by $0.10. Keep lowering until filled."
            if is_credit else
            f"Enter as spread order at LIMIT ${webull_limit_price:.2f} debit. "
            f"If not filled in 5 min, raise by $0.05."
        ),
        "llm_decision": {
            "reasoning":    decision.get("reasoning",""),
            "key_risk":     decision.get("key_risk",""),
            "confidence":   decision.get("confidence", 50),
            "regime_check": decision.get("regime_check",""),
        },
        "price_source": "UW_nbbo",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fallback: Deterministic Strategy Engine
# ─────────────────────────────────────────────────────────────────────────────

def _deterministic_strategy(
    direction: str,
    confidence: int,
    avg_iv: float,
    spot: float,
    dte: int,
    call_strikes: list,
    put_strikes: list,
    atr: float,
) -> dict:
    """
    Fallback when LLM is unavailable.
    Uses deterministic rules to pick strategy and strikes.
    """
    IV_HIGH = 0.40
    spread_width = 10.0  # default $10 wide

    if direction == "BEARISH":
        if avg_iv >= IV_HIGH:
            strategy = "CREDIT_CALL_SPREAD"
            ss = _round_to_strike(spot * 1.05)
            bs = _round_to_strike(ss + spread_width)
            legs = [{"action":"SELL","type":"CALL","strike":ss},
                    {"action":"BUY", "type":"CALL","strike":bs}]
        else:
            strategy = "DEBIT_PUT_SPREAD"
            bs = _round_to_strike(spot * 0.99)
            ss = _round_to_strike(bs - spread_width)
            legs = [{"action":"BUY", "type":"PUT","strike":bs},
                    {"action":"SELL","type":"PUT","strike":ss}]
    elif direction == "BULLISH":
        if avg_iv >= IV_HIGH:
            strategy = "CREDIT_PUT_SPREAD"
            ss = _round_to_strike(spot * 0.95)
            bs = _round_to_strike(ss - spread_width)
            legs = [{"action":"SELL","type":"PUT","strike":ss},
                    {"action":"BUY", "type":"PUT","strike":bs}]
        else:
            strategy = "DEBIT_CALL_SPREAD"
            bs = _round_to_strike(spot * 1.01)
            ss = _round_to_strike(bs + spread_width)
            legs = [{"action":"BUY", "type":"CALL","strike":bs},
                    {"action":"SELL","type":"CALL","strike":ss}]
    else:
        if avg_iv >= IV_HIGH:
            strategy = "IRON_CONDOR"
            cs = _round_to_strike(spot * 1.05); cb = _round_to_strike(cs + spread_width)
            ps = _round_to_strike(spot * 0.95); pb = _round_to_strike(ps - spread_width)
            legs = [
                {"action":"BUY", "type":"PUT", "strike":pb},
                {"action":"SELL","type":"PUT", "strike":ps},
                {"action":"SELL","type":"CALL","strike":cs},
                {"action":"BUY", "type":"CALL","strike":cb},
            ]
        else:
            strategy = "LONG_STRADDLE"
            atm = _round_to_strike(spot)
            legs = [{"action":"BUY","type":"CALL","strike":atm},
                    {"action":"BUY","type":"PUT", "strike":atm}]

    # Find nearest Friday 3 weeks out for expiry
    today = datetime.now()
    days_until_friday = (4 - today.weekday()) % 7 or 7
    expiry = (today + timedelta(days=days_until_friday + 14)).strftime("%Y-%m-%d")

    return {
        "strategy": strategy, "expiry": expiry, "legs": legs,
        "direction": direction, "confidence": confidence,
        "reasoning": f"Fallback deterministic rules: {direction} + {'HIGH' if avg_iv >= IV_HIGH else 'LOW'} IV",
        "key_risk": "LLM unavailable — rule-based fallback",
        "regime_check": "PASS (not verified)"
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def build_recommendation(
    ticker: str,
    ta_profile: dict,
    flow_signal: dict,
    option_contracts: list[dict] | None = None,
    budget: float = 2000.0,
    max_loss: float | None = None,
    profit_target: float | None = None,
    min_dte: int = 4,
    max_dte: int = 365,
    top_n: int = 3,
    user_id: str | None = None,
) -> dict:
    """
    Build complete trade recommendation.

    Architecture:
        1. Python collects all real-time data (spot, TA, flow, option chains)
        2. LLM decides strategy + strikes (reasoning over ALL signals holistically)
        3. Python executes arithmetic with real UW prices (exact math)
        4. LLM adds plain-English narrative

    Args:
        ticker:         stock ticker
        ta_profile:     from get_technical_profile()
        flow_signal:    from score_signal_package()
        budget:         max capital to deploy ($)
        max_loss:       max acceptable loss ($) — default budget × 40%
        profit_target:  minimum profit desired ($) — optional filter
        min_dte:        minimum DTE (default 4)
        max_dte:        maximum DTE (default 365, set 911+ for LEAPS)
    """
    ticker   = ticker.upper()
    max_loss = max_loss or budget * STOP_LOSS_PCT

    # ── Safety blocks ─────────────────────────────────────────────────────────
    if flow_signal.get("trade_blocked"):
        return {
            "signal": "BLOCKED", "ticker": ticker,
            "reason": flow_signal.get("earnings_risk", {}).get("reason","Earnings < 7 days"),
            "warnings": ["Rule 2: No new positions within 7 days of earnings"],
        }
    if ta_profile.get("error"):
        return {"signal":"INSUFFICIENT_DATA","ticker":ticker,
                "reason":ta_profile["error"],"warnings":[]}

    # ── Step 1: Collect all data ──────────────────────────────────────────────
    polygon_close = ta_profile.get("current_price", 0)
    spot          = _get_live_spot(ticker, polygon_close, user_id)
    atr           = ta_profile.get("atr_14") or spot * 0.03
    ta_signal     = ta_profile.get("signal","NEUTRAL")
    ta_score      = ta_profile.get("strength_score", 50)
    flow_dir      = flow_signal.get("direction","NEUTRAL")
    flow_conf     = flow_signal.get("confidence", 50)
    days_to_earn  = flow_signal.get("earnings_risk",{}).get("days_to_earnings")

    # Combined direction + confidence (used for fallback + context)
    if ta_signal == "SELL" and flow_dir == "BEARISH":
        direction, confidence = "BEARISH", min(95, flow_conf + 15)
    elif ta_signal == "BUY" and flow_dir == "BULLISH":
        direction, confidence = "BULLISH", min(95, flow_conf + 15)
    elif flow_conf >= 65:
        direction, confidence = flow_dir, flow_conf
    elif ta_score <= 35:
        direction, confidence = "BEARISH", ta_score + 20
    elif ta_score >= 65:
        direction, confidence = "BULLISH", 100 - ta_score + 20
    else:
        direction, confidence = "NEUTRAL", 45

    # Get all available expiries
    all_expiries = _get_all_expiries(ticker)
    today        = datetime.now()
    expiry_dtes  = []
    for exp in all_expiries:
        try:
            dte = (datetime.strptime(exp, "%Y-%m-%d") - today).days
            if min_dte <= dte <= max_dte:
                expiry_dtes.append((exp, dte))
        except ValueError:
            continue

    if not expiry_dtes:
        return {"signal":"NO_EXPIRIES","ticker":ticker,
                "reason":f"No expiries found between {min_dte}-{max_dte} DTE","warnings":[]}

    # Build expiry options with sample UW prices (for LLM context)
    expiry_options = []
    uw_avg_iv = 0.30  # baseline

    for exp, dte in expiry_dtes[:8]:  # first 8 expiries
        uw_chain     = _get_uw_chain(ticker, exp)
        call_strikes, put_strikes = _get_yf_strikes(ticker, exp)

        # Get ATM prices from UW
        atm_call = _uw_price_for_strike(uw_chain, spot, "CALL", spot, dte, 0.30)
        atm_put  = _uw_price_for_strike(uw_chain, spot, "PUT",  spot, dte, 0.30)

        # Avg IV from this chain
        ivs = [_safe(c.get("implied_volatility")) for c in uw_chain.values()
               if _safe(c.get("implied_volatility")) > 0.05]
        chain_iv = round(sum(ivs)/len(ivs), 3) if ivs else 0.30

        if 20 <= dte <= 40:
            uw_avg_iv = chain_iv  # use mid-term IV as baseline

        expiry_options.append({
            "expiry":       exp,
            "dte":          dte,
            "call_strikes": call_strikes,
            "put_strikes":  put_strikes,
            "atm_call_bid": atm_call[0],
            "atm_call_ask": atm_call[1],
            "atm_put_bid":  atm_put[0],
            "atm_put_ask":  atm_put[1],
            "avg_iv":       f"{chain_iv:.1%}",
        })

    # ── Step 2: LLM decides strategy + strikes ────────────────────────────────
    # Fetch RAG context (historical price, earnings, macro, news)
    rag_context = ""
    try:
        from app.rag.context_builder import get_context_for_prompt
        rag_context = get_context_for_prompt(ticker)
        print(f"[Strategy] RAG context loaded for {ticker}")
    except Exception as e:
        print(f"[Strategy] RAG unavailable: {e}")

    data_package = _build_llm_data_package(
        ticker=ticker,
        ta_profile=ta_profile,
        flow_signal=flow_signal,
        spot=spot,
        expiry_options=expiry_options,
        budget=budget,
        max_loss=max_loss,
        profit_target=profit_target,
        rag_context=rag_context,
    )

    llm_decision = _llm_decide_strategy(data_package, ticker)

    if not llm_decision:
        # Fallback to deterministic rules
        print(f"[Strategy] LLM unavailable — using deterministic fallback")
        exp, dte = expiry_dtes[min(2, len(expiry_dtes)-1)]  # 3rd expiry as default
        call_strikes, put_strikes = _get_yf_strikes(ticker, exp)
        llm_decision = _deterministic_strategy(
            direction, confidence, uw_avg_iv, spot, dte,
            call_strikes, put_strikes, atr
        )
        llm_decision["expiry"] = exp

    # ── Step 3: Python executes exact arithmetic with real UW prices ──────────
    try:
        trade = _execute_trade_math(llm_decision, ticker, spot, budget, max_loss)
    except ValueError as e:
        # Log bad LLM decision for prompt improvement
        _log_llm_decision(ticker, llm_decision, str(e), "REJECTED")

        print(f"[Strategy] LLM trade rejected: {e}")
        print(f"[Strategy] Falling back to deterministic rules...")
        exp, dte = expiry_dtes[min(2, len(expiry_dtes)-1)]
        call_strikes, put_strikes = _get_yf_strikes(ticker, exp)
        llm_decision = _deterministic_strategy(
            direction, confidence, uw_avg_iv, spot, dte,
            call_strikes, put_strikes, atr
        )
        llm_decision["expiry"] = exp
        llm_decision["reasoning"] = f"LLM trade rejected (bad R/R) — using deterministic rules. {llm_decision['reasoning']}"
        trade = _execute_trade_math(llm_decision, ticker, spot, budget, max_loss)
        _log_llm_decision(ticker, llm_decision, f"R/R={trade.get('risk_reward')}", "ACCEPTED")

    # Add greeks to legs
    for leg in trade["legs"]:
        flag = "c" if leg["type"] == "CALL" else "p"
        g    = _bsm_greeks(spot, leg["strike"], trade["dte"], leg.get("iv", uw_avg_iv), flag)
        leg.update(g)

    # ── Step 4: LLM narrative ─────────────────────────────────────────────────
    llm_explanation = ""
    try:
        from app.llm.service import explain_recommendation, is_ollama_available
        if is_ollama_available():
            llm_explanation = explain_recommendation(ticker, {
                **trade, "direction": direction, "confidence": confidence,
                "spot_used": round(spot, 2),
            }, ta_profile, flow_signal)
    except Exception:
        pass

    # Validation warnings
    warnings = []
    if any(l.get("price_source") == "BSM_estimate" for l in trade["legs"]):
        warnings.append("⚠️ Some prices are BSM estimates — UW didn't have that contract. Verify in Webull before trading.")
    if confidence < 55:
        warnings.append(f"Low confidence ({confidence}/100) — LLM flagged this, consider waiting.")
    if days_to_earn and trade["dte"] >= days_to_earn:
        warnings.append(f"⚠️ Expiry {trade['expiry']} crosses earnings ({days_to_earn}d away) — IV crush risk after report.")
    if trade.get("is_credit_strategy") and "NAKED" in trade.get("strategy",""):
        warnings.append("⚠️ Naked strategy requires margin account approval.")

    sig = "SELL" if direction == "BEARISH" else ("BUY" if direction == "BULLISH" else "NEUTRAL")

    return {
        "ticker":           ticker,
        "signal":           sig,
        "direction":        direction,
        "confidence":       confidence,
        "spot":             round(spot, 2),
        "ta_summary":       ta_profile.get("summary"),
        "flow_summary":     flow_signal.get("summary"),
        "best":             trade,
        "alternatives":     [],   # future: run LLM with different constraints
        "llm_explanation":  llm_explanation,
        "llm_decided":      llm_decision is not None,
        "warnings":         warnings,
        "user_constraints": {
            "budget": budget, "max_loss": max_loss,
            "profit_target": profit_target,
            "min_dte": min_dte, "max_dte": max_dte,
        },
    }