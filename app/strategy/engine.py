"""
Strategy Engine v4 (Component C7) — LLM-First Architecture, unified math.

Division of responsibility:
    LLM (Qwen 14B):   Decides strategy, strikes, expiry based on all signals
    Python:           Executes all arithmetic with real-time UW prices

Rewritten July 2026 after live debugging found five real bugs:
  1. R/R gate checked a CONSTANT ratio (TARGET_PROFIT_PCT/STOP_LOSS_PCT)
     for credit trades instead of real economics — let a SPY iron condor
     risking $1,977 to make $23 (86:1, real R/R=0.012) pass undetected,
     because target_profit/stop_loss for any credit trade always reduces
     to roughly TARGET_PROFIT_PCT/STOP_LOSS_PCT regardless of strikes.
  2. Iron condor spread-width math assumed legs arrive in a fixed
     position order (legs_out[0]/[1]) — fragile, silently wrong for
     asymmetric wings, mislabeled in comments.
  3. No sanity ceiling anywhere — historical DB rows show
     max_profit_per_contract in the $592K-$1.69M range on a $2-10K
     budget. Root cause in that (now-rewritten) code is unrecoverable,
     but this must never be possible again regardless of upstream cause.
  4. Two incompatible strategy-naming conventions coexisted with no
     alias resolution (NAKED_CALL vs SELL_NAKED_CALL mean OPPOSITE
     things; STRADDLE vs LONG_STRADDLE are the same thing under two
     names) — unified into one canonical list + alias map for old rows.
  5. Position sizing could silently deploy >budget when even one
     contract's max loss exceeded the stated budget — now rejects.
  6. Option-type matching in UW price lookup used "C" in symbol / "P"
     in symbol — breaks for any ticker whose own letters contain C or P
     (CRM, COIN, CVX, CVNA, PANW, PYPL, PG, CEG, CORZ, etc). Fixed to
     check the actual type-character position (rightmost C/P directly
     before the 8-digit strike field), matching OCC symbol format.

Why LLM for strategy decision, Python for arithmetic: unchanged from v3 —
LLMs weigh signals well but make arithmetic errors with real money.
"""
import json
import math
import re
from datetime import datetime, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TARGET_PROFIT_PCT = 0.80   # "close early" guidance target — NOT used for R/R gating
STOP_LOSS_PCT      = 0.40

# No single option contract's max profit or max loss should ever exceed
# this on a realistic retail budget. Anything above is corrupted data
# (bad width, unit error, price glitch) — reject outright rather than
# let a phantom six/seven-figure number flow into recommendations,
# mark-to-market, or backtest stats.
PER_CONTRACT_SANITY_CAP = 50_000

# Canonical strategy names — used everywhere in this codebase.
STRATEGIES = [
    "NAKED_CALL", "NAKED_PUT",               # buy single option, risk = premium paid
    "SHORT_NAKED_CALL", "SHORT_NAKED_PUT",   # sell single option ⚠️ (call = uncapped risk)
    "DEBIT_CALL_SPREAD", "DEBIT_PUT_SPREAD",
    "CREDIT_CALL_SPREAD", "CREDIT_PUT_SPREAD",
    "IRON_CONDOR",
    "STRADDLE", "STRANGLE",
]

# Legacy names from earlier sessions / historical DB rows — normalized
# on read so nothing downstream has to special-case old data.
STRATEGY_ALIASES = {
    "LONG_STRADDLE":   "STRADDLE",
    "LONG_PUT":        "NAKED_PUT",
    "LONG_CALL":       "NAKED_CALL",
    "SELL_NAKED_CALL": "SHORT_NAKED_CALL",
    "SELL_NAKED_PUT":  "SHORT_NAKED_PUT",
}

CREDIT_STRATEGIES = {
    "CREDIT_CALL_SPREAD", "CREDIT_PUT_SPREAD",
    "SHORT_NAKED_CALL", "SHORT_NAKED_PUT",
    "IRON_CONDOR",
}


def normalize_strategy(strategy: str) -> str:
    return STRATEGY_ALIASES.get(strategy, strategy)


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
            wb = WebullConnector(user_id)
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
    """Fetch UW option contracts for a specific expiry. Returns {symbol: contract}."""
    try:
        from app.options_flow.unusual_whales import get_option_contracts
        contracts = get_option_contracts(ticker, expiry=expiry, limit=500)
        return {c.get("option_symbol",""): c for c in contracts if c.get("option_symbol")}
    except Exception:
        return {}


def _get_yf_strikes(ticker: str, expiry: str) -> tuple[list, list]:
    try:
        import yfinance as yf
        chain = yf.Ticker(ticker).option_chain(expiry)
        return (sorted(chain.calls["strike"].unique().tolist()),
                sorted(chain.puts["strike"].unique().tolist()))
    except Exception:
        return [], []


def _option_type_from_symbol(sym: str) -> str | None:
    """
    Determine CALL/PUT from an OCC option symbol by finding the type
    character in its correct position (immediately before the 8-digit
    strike field), NOT by naive substring search — 'C' in symbol breaks
    for tickers like CRM/COIN/CVX/CVNA/PANW/PYPL/PG/CEG/CORZ, since the
    ticker itself often contains the letters C or P.
    """
    idx_c = sym.rfind("C")
    idx_p = sym.rfind("P")
    if idx_c > idx_p and sym[idx_c+1:].isdigit() and len(sym[idx_c+1:]) == 8:
        return "CALL"
    if idx_p > idx_c and sym[idx_p+1:].isdigit() and len(sym[idx_p+1:]) == 8:
        return "PUT"
    return None


import warnings as _warnings
_warnings.filterwarnings("ignore", category=RuntimeWarning, module="vollib")

def _uw_price_for_strike(
    uw_chain: dict,
    strike: float,
    option_type: str,
    spot: float,
    dte: int,
    avg_iv: float,
    max_diff: float = 2.5,
) -> tuple[float, float, float, str]:
    """Get UW bid/ask for a specific strike. Returns (bid, ask, iv, source)."""
    best = None
    best_diff = float("inf")

    for sym, c in uw_chain.items():
        if _option_type_from_symbol(sym) != option_type:
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


def _group_legs(legs_out: list[dict]) -> dict[str, list[dict]]:
    """Group legs by TYPE+ACTION — robust to whatever order they arrive in."""
    groups: dict[str, list[dict]] = {"CALL_BUY": [], "CALL_SELL": [], "PUT_BUY": [], "PUT_SELL": []}
    for leg in legs_out:
        key = f"{leg['type']}_{leg['action']}"
        if key in groups:
            groups[key].append(leg)
    return groups


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — no scipy dependency needed."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _prob_above(spot: float, strike: float, dte: int, iv: float, r: float = 0.05) -> float:
    """
    Risk-neutral probability that S_T > strike at expiration — the
    standard N(d2) 'probability ITM' measure used by real platforms
    (thinkorswim, tastytrade). Distinct from delta (N(d1), a hedge
    ratio, not a probability), though numerically close in practice.
    """
    if strike <= 0 or spot <= 0 or iv <= 0:
        return 0.5
    t = max(dte, 1) / 365.0
    try:
        d2 = (math.log(spot / strike) + (r - 0.5 * iv ** 2) * t) / (iv * math.sqrt(t))
        return _norm_cdf(d2)
    except (ValueError, ZeroDivisionError):
        return 0.5


def _find_leg(legs_out: list[dict], type_: str, action: str) -> dict | None:
    for l in legs_out:
        if l["type"] == type_ and l["action"] == action:
            return l
    return None


def _estimate_pop_and_ev(
    strategy: str, legs_out: list[dict], spot: float, dte: int, avg_iv: float,
    entry: float, max_p_c: float, max_l_c: float,
) -> tuple[float | None, float | None]:
    """
    Estimate probability of profit (BSM N(d2)) and a simplified bimodal
    expected value: EV = POP*max_gain - (1-POP)*max_loss.

    This is a real-money gate for long-premium (debit) strategies — see
    is_credit exclusion at the call site. Bimodal EV is a standard
    retail-analytics simplification (treats the outcome as roughly
    max-gain or roughly max-loss, ignoring the partial-profit region
    between breakevens) — directionally reliable, not a precise
    integral of the full P&L distribution.

    Returns (None, None) if the strategy/leg structure isn't recognized
    — callers should skip the gate rather than reject on missing data.
    """
    def pop_above(k): return _prob_above(spot, k, dte, avg_iv)

    pop = None

    if strategy == "IRON_CONDOR":
        call_short = _find_leg(legs_out, "CALL", "SELL")
        put_short  = _find_leg(legs_out, "PUT",  "SELL")
        if call_short and put_short:
            credit  = abs(entry)
            call_be = call_short["strike"] + credit
            put_be  = put_short["strike"]  - credit
            pop = pop_above(put_be) - pop_above(call_be)

    elif strategy == "DEBIT_CALL_SPREAD":
        buy_leg = _find_leg(legs_out, "CALL", "BUY")
        if buy_leg:
            pop = pop_above(buy_leg["strike"] + entry)

    elif strategy == "CREDIT_PUT_SPREAD":
        sell_leg = _find_leg(legs_out, "PUT", "SELL")
        if sell_leg:
            pop = pop_above(sell_leg["strike"] - abs(entry))

    elif strategy == "DEBIT_PUT_SPREAD":
        buy_leg = _find_leg(legs_out, "PUT", "BUY")
        if buy_leg:
            pop = 1 - pop_above(buy_leg["strike"] - entry)

    elif strategy == "CREDIT_CALL_SPREAD":
        sell_leg = _find_leg(legs_out, "CALL", "SELL")
        if sell_leg:
            pop = 1 - pop_above(sell_leg["strike"] + abs(entry))

    elif strategy in ("STRADDLE", "STRANGLE"):
        call_leg = _find_leg(legs_out, "CALL", "BUY")
        put_leg  = _find_leg(legs_out, "PUT",  "BUY")
        if call_leg and put_leg:
            call_be = call_leg["strike"] + entry
            put_be  = put_leg["strike"]  - entry
            pop = pop_above(call_be) + (1 - pop_above(put_be))

    elif strategy == "NAKED_CALL":
        buy_leg = _find_leg(legs_out, "CALL", "BUY")
        if buy_leg:
            pop = pop_above(buy_leg["strike"] + entry)

    elif strategy == "NAKED_PUT":
        buy_leg = _find_leg(legs_out, "PUT", "BUY")
        if buy_leg:
            pop = 1 - pop_above(buy_leg["strike"] - entry)

    elif strategy == "SHORT_NAKED_CALL":
        sell_leg = _find_leg(legs_out, "CALL", "SELL")
        if sell_leg:
            pop = 1 - pop_above(sell_leg["strike"] + abs(entry))

    elif strategy == "SHORT_NAKED_PUT":
        sell_leg = _find_leg(legs_out, "PUT", "SELL")
        if sell_leg:
            pop = pop_above(sell_leg["strike"] - abs(entry))

    if pop is None:
        return None, None

    pop = min(max(pop, 0.0), 1.0)
    ev  = round(pop * max_p_c - (1 - pop) * max_l_c, 2)
    return round(pop, 4), ev


# ─────────────────────────────────────────────────────────────────────────────
# Data Package for LLM (single-ticker path — build_recommendation)
# ─────────────────────────────────────────────────────────────────────────────

def _build_llm_data_package(
    ticker: str, ta_profile: dict, flow_signal: dict, spot: float,
    expiry_options: list[dict], budget: float, max_loss: float,
    profit_target: float | None, rag_context: str = "",
) -> str:
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

    expiry_str = "\nAVAILABLE OPTION EXPIRIES (with sample UW prices):"
    for e in expiry_options[:8]:
        expiry_str += f"\n  {e['expiry']} ({e['dte']} DTE) — ATM call bid/ask: {e.get('atm_call_bid','?')}/{e.get('atm_call_ask','?')} | ATM put: {e.get('atm_put_bid','?')}/{e.get('atm_put_ask','?')} | IV: {e.get('avg_iv','?')}"
        expiry_str += f"\n    Call strikes: {e.get('call_strikes', [])[:6]}"
        expiry_str += f"\n    Put strikes:  {e.get('put_strikes', [])[:6]}"

    constraints = f"""
USER CONSTRAINTS
  Budget:         ${budget} (total to deploy)
  Max loss:       ${max_loss} (stop loss — exit if position loses this)
  Profit target:  ${profit_target or 'no minimum'} (minimum desired profit)
  Risk tolerance: {'conservative' if max_loss < budget * 0.3 else 'moderate' if max_loss < budget * 0.5 else 'aggressive'}"""

    rules = """
TRADING RULES (must be followed)
  Rule 1: Close at 80% of max profit OR 40% loss of premium paid
  Rule 2: No new positions if earnings within 7 days (check earnings_risk)
  Rule 3: Regime check — only trade in confirmed direction

AVAILABLE STRATEGIES
  NAKED_CALL:         Buy single call — very bullish, low IV, risk = premium
  NAKED_PUT:          Buy single put — very bearish, low IV, risk = premium
  DEBIT_CALL_SPREAD:  Buy lower call, sell higher call — bullish, defined risk, low/med IV
  DEBIT_PUT_SPREAD:   Buy higher put, sell lower put — bearish, defined risk, low/med IV
  CREDIT_CALL_SPREAD: Sell lower call, buy higher call — bearish, high IV, collect premium
  CREDIT_PUT_SPREAD:  Sell higher put, buy lower put — bullish, high IV, collect premium
  IRON_CONDOR:        Sell call + put spread both sides — neutral, high IV
  STRADDLE:           Buy call + put ATM — neutral, expect big move, either direction
  STRANGLE:           Buy OTM call + OTM put — cheaper than straddle, needs bigger move
  SHORT_NAKED_CALL:   Sell single call ⚠️ UNCAPPED RISK — very bearish, very high IV
  SHORT_NAKED_PUT:    Sell single put ⚠️ — very bullish, very high IV, cash secured

PUT SPREAD RULE: sell strike MUST be higher than buy strike (sell closer to ATM, buy further OTM)
CALL SPREAD RULE: buy strike MUST be higher than sell strike (sell closer to ATM, buy further OTM)
IRON CONDOR LEG ORDER (Webull convention): BUY PUT (lowest) → SELL PUT → SELL CALL → BUY CALL (highest)"""

    rag_section = f"\n\nMARKET CONTEXT (Historical + News + Macro):\n{rag_context[:2000]}" if rag_context else ""

    return ta + flow + expiry_str + constraints + rules + rag_section


def _log_llm_decision(ticker: str, decision: dict, outcome: str, status: str) -> None:
    from pathlib import Path
    log_dir = Path(__file__).resolve().parents[2] / "logs"
    log_dir.mkdir(exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(), "ticker": ticker, "status": status,
        "outcome": outcome, "strategy": decision.get("strategy"), "expiry": decision.get("expiry"),
        "direction": decision.get("direction"), "confidence": decision.get("confidence"),
        "legs": decision.get("legs", []), "reasoning": decision.get("reasoning", ""),
        "key_news": decision.get("key_news", "NONE"),
    }
    try:
        with open(log_dir / "llm_decisions.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# LLM Strategy Decision (single-ticker path)
# ─────────────────────────────────────────────────────────────────────────────

def _llm_decide_strategy(data_package: str, ticker: str) -> dict | None:
    system = """You are an expert options trader. Select the optimal strategy and OTM strikes.
Respond with valid JSON ONLY — no text before or after the JSON.

JSON format:
{
  "strategy": "DEBIT_PUT_SPREAD",
  "expiry": "YYYY-MM-DD from AVAILABLE OPTION EXPIRIES list only",
  "legs": [
    {"action": "BUY", "type": "PUT", "strike": 205.0},
    {"action": "SELL", "type": "PUT", "strike": 195.0}
  ],
  "direction": "BEARISH",
  "confidence": 65,
  "reasoning": "2-3 sentences on why this trade",
  "key_risk": "1 sentence on main risk",
  "key_news": "1-2 global headlines that influenced this recommendation, or NONE",
  "regime_check": "PASS or FAIL with reason"
}

CRITICAL EXPIRY RULE: use ONLY dates from the AVAILABLE OPTION EXPIRIES list. Do NOT invent dates.

STRATEGIES: NAKED_CALL, NAKED_PUT, DEBIT_CALL_SPREAD, DEBIT_PUT_SPREAD, CREDIT_CALL_SPREAD,
CREDIT_PUT_SPREAD, IRON_CONDOR, STRADDLE, STRANGLE, SHORT_NAKED_CALL, SHORT_NAKED_PUT.

CRITICAL STRIKE RULES — ALWAYS OTM/ATM ONLY:
- DEBIT_PUT_SPREAD (bearish): BUY 0-3% below spot, SELL 5-10% below spot
- DEBIT_CALL_SPREAD (bullish): BUY 0-3% above spot, SELL 5-10% above spot
- CREDIT_CALL_SPREAD (bearish): SELL 3-8% above spot, BUY 8-13% above spot
- CREDIT_PUT_SPREAD (bullish): SELL 3-8% below spot, BUY 8-13% below spot
- IRON_CONDOR (neutral): short strikes 3-6% each side from spot

NEVER pick deeply ITM strikes (>10% ITM). Minimum required R/R = 0.5 for debit trades,
0.15 for credit trades (checked against REAL max_gain/max_loss, not a target/stop ratio).

Strike ordering:
- PUT SPREAD: sell_strike < buy_strike (sell is lower/further OTM)
- CALL SPREAD: buy_strike > sell_strike (buy is higher/further OTM)
- IRON CONDOR: BUY PUT(lowest) → SELL PUT → SELL CALL → BUY CALL(highest)"""

    from app.utils.config import settings
    import requests as req

    def _call(prompt_text: str, max_tokens: int = 600) -> dict | None:
        try:
            payload = {
                "model": settings.ollama_model, "prompt": prompt_text, "system": system,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.1, "top_p": 0.9, "num_ctx": 4096},
            }
            r = req.post(f"{settings.ollama_host}/api/generate", json=payload, timeout=120)
            r.raise_for_status()
            raw = r.json().get("response", "").strip()
            json_match = re.search(r'\{[^{}]*"strategy"[^{}]*\}', raw, re.DOTALL) or re.search(r'\{.*\}', raw, re.DOTALL)
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

    result = _call(f"Analyze this {ticker} options trade and select the best strategy:\n\n{data_package[:3500]}")
    if result:
        return result

    lines = data_package.split('\n')
    key_lines = [l for l in lines if any(k in l for k in
        ['Direction', 'Confidence', 'Signal', 'RSI', 'MACD', 'trend', 'TREND',
         'GEX', 'tide', 'Earnings', 'Budget', 'Max loss', 'expiry', 'DTE',
         'call strikes', 'put strikes', 'ATM call', 'IV:'])]
    mini_package = '\n'.join(key_lines[:40])
    print(f"[LLM] Retrying with minimal prompt ({len(mini_package)} chars)...")
    result = _call(f"Select the best options strategy for {ticker}.\n\nKey data:\n{mini_package}\n\nPick strategy, expiry, and strikes. Respond with JSON only.", max_tokens=400)
    if result:
        return result

    print(f"[LLM] Both attempts failed — using deterministic fallback")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Python Arithmetic Executor — the core fix lives here
# ─────────────────────────────────────────────────────────────────────────────

def _execute_trade_math(
    decision: dict, ticker: str, spot: float, budget: float, max_loss: float,
) -> dict:
    """
    Given LLM's strategy decision, execute all arithmetic with real UW prices.
    Position-independent leg grouping, real R/R gating, hard sanity cap.
    """
    strategy = normalize_strategy(decision["strategy"])
    expiry   = decision["expiry"]
    legs_in  = decision["legs"]
    dte      = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days

    uw_chain = _get_uw_chain(ticker, expiry)
    ivs = [_safe(c.get("implied_volatility")) for c in uw_chain.values()
           if _safe(c.get("implied_volatility")) > 0.05]
    avg_iv = sum(ivs) / len(ivs) if ivs else 0.30

    is_credit = strategy in CREDIT_STRATEGIES

    legs_out = []
    for leg in legs_in:
        strike   = float(leg["strike"])
        opt_type = leg["type"]
        action   = leg["action"]
        bid, ask, iv, source = _uw_price_for_strike(uw_chain, strike, opt_type, spot, dte, avg_iv)
        flag = "c" if opt_type == "CALL" else "p"
        greeks = _bsm_greeks(spot, strike, dte, iv, flag)
        legs_out.append({
            "action": action, "type": opt_type, "strike": strike, "expiry": expiry,
            "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 2),
            "iv": round(iv, 3), "price_source": source, **greeks,
        })

    entry = 0.0
    for leg in legs_out:
        entry += leg["ask"] if leg["action"] == "BUY" else -leg["bid"]
    entry = round(entry, 2)

    groups = _group_legs(legs_out)

    # ── Width computation — by leg TYPE, never by position ────────────────
    call_width = put_width = spread_width = 0.0
    if strategy == "IRON_CONDOR":
        if groups["CALL_BUY"] and groups["CALL_SELL"]:
            call_width = abs(groups["CALL_BUY"][0]["strike"] - groups["CALL_SELL"][0]["strike"])
        if groups["PUT_BUY"] and groups["PUT_SELL"]:
            put_width = abs(groups["PUT_BUY"][0]["strike"] - groups["PUT_SELL"][0]["strike"])
        if not (call_width and put_width) and len(legs_out) == 4:
            # Defensive fallback only if type-grouping somehow failed
            call_width = put_width = abs(legs_out[1]["strike"] - legs_out[0]["strike"])
        spread_width = max(call_width, put_width)   # worst-case wing drives max loss
    elif len(legs_out) == 2 and strategy not in ("STRADDLE", "STRANGLE"):
        spread_width = abs(legs_out[0]["strike"] - legs_out[1]["strike"])

    # ── Max profit / max loss per contract ─────────────────────────────────
    max_profit_is_estimate = False

    if strategy in ("STRADDLE", "STRANGLE"):
        # Always a debit (buy both legs). Max loss = premium paid (defined).
        # Max profit is technically unbounded on the call side — estimate
        # using a 1-standard-deviation expected move from IV, clearly
        # flagged as an ESTIMATE, never treated as a hard ceiling.
        max_l_c = round(entry * 100, 2)
        expected_move = spot * avg_iv * math.sqrt(max(dte, 1) / 365.0)
        call_leg = next((l for l in legs_out if l["type"] == "CALL"), None)
        put_leg  = next((l for l in legs_out if l["type"] == "PUT"),  None)
        up_price, down_price = spot + expected_move, max(spot - expected_move, 0)
        up_payoff   = max(up_price - call_leg["strike"], 0) if call_leg else 0
        down_payoff = max(put_leg["strike"] - down_price, 0) if put_leg else 0
        max_p_c = max(round((max(up_payoff, down_payoff) - entry) * 100, 2), 1.0)
        max_profit_is_estimate = True
        size_by = max(max_l_c, 1)

    elif is_credit:
        credit  = abs(entry)
        max_p_c = round(credit * 100, 2)
        if spread_width > 0:
            max_l_c = round((spread_width - credit) * 100, 2)
        else:
            max_l_c = round(spot * 100, 2)   # naked — capped display near spot
        size_by = max(max_l_c, 1)

    else:
        # Debit spreads and single-leg NAKED_CALL/NAKED_PUT
        if spread_width > 0:
            max_p_c = round((spread_width - entry) * 100, 2)
        else:
            max_p_c = round(spot * 0.20 * 100, 2)   # naked long — rough estimate
            max_profit_is_estimate = True
        max_l_c = round(entry * 100, 2)
        size_by = max(max_l_c, 1)

    # ── Sanity cap — reject corrupted math outright ────────────────────────
    if abs(max_p_c) > PER_CONTRACT_SANITY_CAP or abs(max_l_c) > PER_CONTRACT_SANITY_CAP:
        raise ValueError(
            f"Sanity cap triggered: max_profit_per_contract={max_p_c} "
            f"max_loss_per_contract={max_l_c} exceeds ${PER_CONTRACT_SANITY_CAP:,} — "
            f"rejecting as corrupted trade math. entry={entry} width={spread_width}"
        )

    # ── Position sizing — never silently exceed budget ─────────────────────
    if size_by > budget * 1.1:
        raise ValueError(
            f"Position too large for budget: 1 contract risks ${size_by:,.0f} "
            f"against a ${budget:,.0f} budget."
        )
    n = max(1, int(budget / size_by))
    while (size_by * n) > budget and n > 1:
        n -= 1

    # ── P&L rollup ──────────────────────────────────────────────────────────
    if is_credit:
        credit_received  = round(abs(entry) * 100 * n, 2)
        margin_required   = round(max_l_c * n, 2)
        target_profit     = round(credit_received * TARGET_PROFIT_PCT, 2)
        stop_loss         = round(credit_received * STOP_LOSS_PCT, 2)
        total_cost        = credit_received
        pnl_label         = "credit_received"
        real_risk_reward  = round(credit_received / margin_required, 4) if margin_required > 0 else None
    else:
        premium_paid      = round(abs(entry) * 100 * n, 2)
        margin_required   = premium_paid
        target_profit     = round(max_p_c * TARGET_PROFIT_PCT * n, 2)
        stop_loss         = round(premium_paid * STOP_LOSS_PCT, 2)
        total_cost        = premium_paid
        pnl_label         = "premium_paid"
        credit_received   = 0
        real_risk_reward  = round((max_p_c * n) / premium_paid, 4) if premium_paid > 0 else None

    # ── R/R validation — REAL economics gate the trade, period ─────────────
    _rr_min = 0.5
    if dte and dte >= 60:   _rr_min = 0.20
    elif dte and dte >= 30: _rr_min = 0.30
    elif dte and dte >= 14: _rr_min = 0.40

    # Credit trades (selling premium) have structurally different economics
    # — a well-built iron condor typically shows real R/R around 0.15-0.50,
    # nothing like a debit spread. Flat floor, not scaled off debit buckets.
    _rr_min_check = 0.15 if is_credit else _rr_min

    if real_risk_reward is not None and real_risk_reward < _rr_min_check:
        strikes = [l["strike"] for l in legs_out]
        gain = target_profit if not is_credit else round(max_p_c * n, 2)
        loss = stop_loss if not is_credit else round(max_l_c * n, 2)
        raise ValueError(
            f"Real R/R {real_risk_reward} (gain=${gain:,.0f} vs loss=${loss:,.0f}) "
            f"below {_rr_min_check} — chosen strikes {strikes} rejected. Triggering fallback."
        )

    # ── Probability-adjusted EV gate — long-premium strategies only ────────
    # A trade can clear the R/R floor and still be a bad bet if the real
    # probability of reaching that gain is far below what the ratio needs.
    # Applied only to debit/long-premium strategies (NAKED_CALL/PUT, DEBIT
    # spreads, STRADDLE, STRANGLE): for credit strategies, an N(d2) estimate
    # using the SAME IV used to price the trade is known to be systematically
    # pessimistic (the volatility risk premium — IV usually overstates
    # realized vol — is exactly where premium-selling's real edge comes
    # from, and this calculation can't see it). pop/ev are still computed
    # and returned for every strategy for visibility; only the reject is
    # scoped to avoid quietly filtering out the one strategy class
    # (IRON_CONDOR) this system's own backtest shows actually works.
    pop_estimate, expected_value = _estimate_pop_and_ev(
        strategy, legs_out, spot, dte, avg_iv, entry, max_p_c, max_l_c
    )
    if not is_credit and expected_value is not None and expected_value < 0:
        strikes = [l["strike"] for l in legs_out]
        raise ValueError(
            f"Negative estimated EV ${expected_value:,.0f} (POP={pop_estimate:.1%}, "
            f"max_gain=${max_p_c:,.0f}, max_loss=${max_l_c:,.0f}) — "
            f"chosen strikes {strikes} rejected. Triggering fallback."
        )

    sell_mid = sum(l["mid"] for l in legs_out if l["action"] == "SELL")
    buy_mid  = sum(l["mid"] for l in legs_out if l["action"] == "BUY")
    webull_limit_price = round(sell_mid - buy_mid, 2) if is_credit else round(buy_mid - sell_mid, 2)

    engine_warnings = []
    if max_profit_is_estimate:
        engine_warnings.append("Max profit is an ESTIMATE (1σ expected move), not a hard ceiling.")
    if strategy == "SHORT_NAKED_CALL":
        engine_warnings.append("⚠️ UNCAPPED RISK — naked short call has theoretically unlimited max loss.")

    return {
        "strategy":              strategy,
        "expiry":                expiry,
        "dte":                   dte,
        "is_credit_strategy":    is_credit,
        "pnl_label":             pnl_label,
        "legs":                  legs_out,
        "spread_width":          spread_width,
        "call_width":            call_width,
        "put_width":             put_width,
        "entry_debit":           entry,
        "avg_iv":                round(avg_iv, 3),
        "contracts":             n,
        "total_cost":            total_cost,
        "credit_received":       credit_received if is_credit else 0,
        "premium_paid":          total_cost if not is_credit else 0,
        "margin_required":       margin_required,
        "max_loss_per_contract": max_l_c,
        "max_profit_per_contract": max_p_c,
        "max_profit_is_estimate": max_profit_is_estimate,
        "target_profit":         target_profit,
        "stop_loss":             stop_loss,
        "risk_reward":           real_risk_reward,   # the ONLY r/r value now — no shadow constant
        "pop_estimate":          pop_estimate,
        "expected_value":        expected_value,
        "webull_limit_price":    webull_limit_price,
        "webull_instructions": (
            f"Enter as Iron Condor/Spread order at LIMIT ${webull_limit_price:.2f} credit. "
            f"If not filled in 5 min, lower by $0.10. Keep lowering until filled."
            if is_credit else
            f"Enter as spread order at LIMIT ${webull_limit_price:.2f} debit. "
            f"If not filled in 5 min, raise by $0.05."
        ),
        "engine_warnings": engine_warnings,
        "llm_decision": {
            "reasoning":    decision.get("reasoning",""),
            "key_news":     decision.get("key_news","NONE"),
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
    direction: str, confidence: int, avg_iv: float, spot: float, dte: int,
    call_strikes: list, put_strikes: list, atr: float,
) -> dict:
    IV_HIGH = 0.40
    spread_width = 10.0

    if direction == "BEARISH":
        if avg_iv >= IV_HIGH:
            strategy = "CREDIT_CALL_SPREAD"
            ss = _round_to_strike(spot * 1.05); bs = _round_to_strike(ss + spread_width)
            legs = [{"action":"SELL","type":"CALL","strike":ss}, {"action":"BUY","type":"CALL","strike":bs}]
        else:
            strategy = "DEBIT_PUT_SPREAD"
            bs = _round_to_strike(spot * 0.99); ss = _round_to_strike(bs - spread_width)
            legs = [{"action":"BUY","type":"PUT","strike":bs}, {"action":"SELL","type":"PUT","strike":ss}]
    elif direction == "BULLISH":
        if avg_iv >= IV_HIGH:
            strategy = "CREDIT_PUT_SPREAD"
            ss = _round_to_strike(spot * 0.95); bs = _round_to_strike(ss - spread_width)
            legs = [{"action":"SELL","type":"PUT","strike":ss}, {"action":"BUY","type":"PUT","strike":bs}]
        else:
            strategy = "DEBIT_CALL_SPREAD"
            bs = _round_to_strike(spot * 1.01); ss = _round_to_strike(bs + spread_width)
            legs = [{"action":"BUY","type":"CALL","strike":bs}, {"action":"SELL","type":"CALL","strike":ss}]
    else:
        if avg_iv >= IV_HIGH:
            strategy = "IRON_CONDOR"
            cs = _round_to_strike(spot * 1.05); cb = _round_to_strike(cs + spread_width)
            ps = _round_to_strike(spot * 0.95); pb = _round_to_strike(ps - spread_width)
            legs = [
                {"action":"BUY","type":"PUT","strike":pb}, {"action":"SELL","type":"PUT","strike":ps},
                {"action":"SELL","type":"CALL","strike":cs}, {"action":"BUY","type":"CALL","strike":cb},
            ]
        else:
            strategy = "STRADDLE"
            atm = _round_to_strike(spot)
            legs = [{"action":"BUY","type":"CALL","strike":atm}, {"action":"BUY","type":"PUT","strike":atm}]

    today = datetime.now()
    days_until_friday = (4 - today.weekday()) % 7 or 7
    expiry = (today + timedelta(days=days_until_friday + 14)).strftime("%Y-%m-%d")

    return {
        "strategy": strategy, "expiry": expiry, "legs": legs,
        "direction": direction, "confidence": confidence,
        "reasoning": f"Fallback deterministic rules: {direction} + {'HIGH' if avg_iv >= IV_HIGH else 'LOW'} IV",
        "key_news": "NONE — LLM unavailable",
        "key_risk": "LLM unavailable — rule-based fallback",
        "regime_check": "PASS (not verified)",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def build_recommendation(
    ticker: str, ta_profile: dict, flow_signal: dict,
    option_contracts: list[dict] | None = None, budget: float = 2000.0,
    max_loss: float | None = None, profit_target: float | None = None,
    min_dte: int = 4, max_dte: int = 365, top_n: int = 3, user_id: str | None = None,
) -> dict:
    ticker   = ticker.upper()
    max_loss = max_loss or budget * STOP_LOSS_PCT

    if flow_signal.get("trade_blocked"):
        return {"signal": "BLOCKED", "ticker": ticker,
                "reason": flow_signal.get("earnings_risk", {}).get("reason","Earnings < 7 days"),
                "warnings": ["Rule 2: No new positions within 7 days of earnings"]}
    if ta_profile.get("error"):
        return {"signal":"INSUFFICIENT_DATA","ticker":ticker,"reason":ta_profile["error"],"warnings":[]}

    polygon_close = ta_profile.get("current_price", 0)
    spot          = _get_live_spot(ticker, polygon_close, user_id)
    atr           = ta_profile.get("atr_14") or spot * 0.03
    ta_signal     = ta_profile.get("signal","NEUTRAL")
    ta_score      = ta_profile.get("strength_score", 50)
    flow_dir      = flow_signal.get("direction","NEUTRAL")
    flow_conf     = flow_signal.get("confidence", 50)
    days_to_earn  = flow_signal.get("earnings_risk",{}).get("days_to_earnings")

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

    expiry_options = []
    uw_avg_iv = 0.30
    for exp, dte in expiry_dtes[:8]:
        uw_chain = _get_uw_chain(ticker, exp)
        call_strikes, put_strikes = _get_yf_strikes(ticker, exp)
        atm_call = _uw_price_for_strike(uw_chain, spot, "CALL", spot, dte, 0.30)
        atm_put  = _uw_price_for_strike(uw_chain, spot, "PUT",  spot, dte, 0.30)
        ivs = [_safe(c.get("implied_volatility")) for c in uw_chain.values()
               if _safe(c.get("implied_volatility")) > 0.05]
        chain_iv = round(sum(ivs)/len(ivs), 3) if ivs else 0.30
        if 20 <= dte <= 40:
            uw_avg_iv = chain_iv
        expiry_options.append({
            "expiry": exp, "dte": dte, "call_strikes": call_strikes, "put_strikes": put_strikes,
            "atm_call_bid": atm_call[0], "atm_call_ask": atm_call[1],
            "atm_put_bid": atm_put[0], "atm_put_ask": atm_put[1], "avg_iv": f"{chain_iv:.1%}",
        })

    valid_expiry_set = {exp for exp, _ in expiry_dtes}

    rag_context = ""
    try:
        from app.rag.context_builder import get_context_for_prompt
        rag_context = get_context_for_prompt(ticker)
    except Exception as e:
        print(f"[Strategy] RAG unavailable: {e}")

    data_package = _build_llm_data_package(
        ticker=ticker, ta_profile=ta_profile, flow_signal=flow_signal, spot=spot,
        expiry_options=expiry_options, budget=budget, max_loss=max_loss,
        profit_target=profit_target, rag_context=rag_context,
    )

    llm_decision = _llm_decide_strategy(data_package, ticker)

    if not llm_decision:
        print(f"[Strategy] LLM unavailable — using deterministic fallback")
        exp, dte = expiry_dtes[min(2, len(expiry_dtes)-1)]
        call_strikes, put_strikes = _get_yf_strikes(ticker, exp)
        llm_decision = _deterministic_strategy(direction, confidence, uw_avg_iv, spot, dte, call_strikes, put_strikes, atr)
        llm_decision["expiry"] = exp

    if llm_decision and llm_decision.get("expiry"):
        chosen_exp = llm_decision["expiry"]
        if chosen_exp not in valid_expiry_set:
            corrected_exp, corrected_dte = expiry_dtes[min(1, len(expiry_dtes)-1)]
            print(f"[Strategy] LLM chose {chosen_exp} outside range — correcting to {corrected_exp} ({corrected_dte} DTE)")
            llm_decision["expiry"] = corrected_exp
            llm_decision["dte"]    = corrected_dte

    try:
        trade = _execute_trade_math(llm_decision, ticker, spot, budget, max_loss)
    except ValueError as e:
        _log_llm_decision(ticker, llm_decision, str(e), "REJECTED")
        print(f"[Strategy] LLM trade rejected: {e}")
        print(f"[Strategy] Falling back to deterministic rules...")
        exp, dte = expiry_dtes[min(2, len(expiry_dtes)-1)]
        call_strikes, put_strikes = _get_yf_strikes(ticker, exp)
        llm_decision = _deterministic_strategy(direction, confidence, uw_avg_iv, spot, dte, call_strikes, put_strikes, atr)
        llm_decision["expiry"] = exp
        llm_decision["reasoning"] = f"LLM trade rejected (bad R/R) — using deterministic rules. {llm_decision['reasoning']}"
        trade = _execute_trade_math(llm_decision, ticker, spot, budget, max_loss)
        _log_llm_decision(ticker, llm_decision, f"R/R={trade.get('risk_reward')}", "ACCEPTED")

    for leg in trade["legs"]:
        flag = "c" if leg["type"] == "CALL" else "p"
        g    = _bsm_greeks(spot, leg["strike"], trade["dte"], leg.get("iv", uw_avg_iv), flag)
        leg.update(g)

    llm_explanation = ""
    try:
        from app.llm.service import explain_recommendation, is_ollama_available
        if is_ollama_available():
            llm_explanation = explain_recommendation(ticker, {
                **trade, "direction": direction, "confidence": confidence, "spot_used": round(spot, 2),
            }, ta_profile, flow_signal)
    except Exception:
        pass

    warnings = list(trade.get("engine_warnings", []))
    if any(l.get("price_source") == "BSM_estimate" for l in trade["legs"]):
        warnings.append("⚠️ Some prices are BSM estimates — UW didn't have that contract. Verify in Webull before trading.")
    if confidence < 55:
        warnings.append(f"Low confidence ({confidence}/100) — LLM flagged this, consider waiting.")
    if days_to_earn and trade["dte"] >= days_to_earn:
        warnings.append(f"⚠️ Expiry {trade['expiry']} crosses earnings ({days_to_earn}d away) — IV crush risk after report.")

    sig = "SELL" if direction == "BEARISH" else ("BUY" if direction == "BULLISH" else "NEUTRAL")

    return {
        "ticker": ticker, "signal": sig, "direction": direction, "confidence": confidence,
        "spot": round(spot, 2), "ta_summary": ta_profile.get("summary"),
        "flow_summary": flow_signal.get("summary"), "best": trade, "alternatives": [],
        "llm_explanation": llm_explanation, "llm_decided": llm_decision is not None,
        "warnings": warnings,
        "user_constraints": {"budget": budget, "max_loss": max_loss, "profit_target": profit_target,
                              "min_dte": min_dte, "max_dte": max_dte},
    }
