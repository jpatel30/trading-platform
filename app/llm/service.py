"""
Local LLM Service (Component C9).

Uses Ollama + Qwen2.5:14b for:
    1. Trade recommendation narrative (plain English explanation)
    2. Signal summarization (why these signals matter)
    3. Risk/reward analysis in conversational language

NOT used for:
    - Scanning (pure computation)
    - Technical indicators (math)
    - Options pricing (formulas)
    - Signal scoring (deterministic rules)

The LLM runs locally on MacBook M5 Pro 48GB — zero cloud cost,
zero latency penalty vs API calls, full privacy.

Usage:
    from app.llm.service import explain_recommendation, summarize_signals

    narrative = explain_recommendation(ticker, rec, ta_profile, flow_signal)
"""
import json
from app.utils.config import settings


def _call_ollama(prompt: str, system: str = "", max_tokens: int = 500) -> str:
    """Call local Ollama API with Qwen2.5:14b."""
    try:
        import requests
        payload = {
            "model": settings.ollama_model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": 0.3,   # low temp for consistent, factual output
                "top_p": 0.9,
            }
        }
        r = requests.post(
            f"{settings.ollama_host}/api/generate",
            json=payload,
            timeout=60
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        return f"[LLM unavailable: {e}]"


def explain_recommendation(
    ticker: str,
    rec: dict,
    ta_profile: dict,
    flow_signal: dict,
) -> str:
    """
    Generate a plain-English narrative explaining the trade recommendation.

    Called after build_recommendation() to add the LLM explanation layer.
    The LLM explains WHY the signals are significant and what to watch for.
    """
    system = """You are a concise professional options trading analyst.
Given trade data, write a 3-4 sentence explanation covering:
1. Why the signals point to this direction
2. What makes this strategy appropriate for the current conditions
3. The key risk to watch for
Be direct, specific, and use exact numbers from the data. No generic advice."""

    # Build a concise fact sheet for the LLM
    facts = f"""
TICKER: {ticker} @ ${rec.get('spot_used', 0)}
STRATEGY: {rec.get('strategy')} | {rec.get('direction')} | Confidence: {rec.get('confidence')}/100
IV Environment: {rec.get('iv_environment')} ({rec.get('avg_iv', 0)*100:.1f}%)
Timeframe: {rec.get('timeframe')} | Expiry: {rec.get('expiry')} ({rec.get('dte')} DTE)

TRADE:
- Entry cost: ${rec.get('total_cost')} for {rec.get('contracts')} contract(s)
- Target: +${rec.get('target_profit')} | Stop: -${rec.get('stop_loss')} | R/R: {rec.get('risk_reward')}
- Rule: {rec.get('rule_used')}

TA SIGNALS:
- {ta_profile.get('summary', 'N/A')}
- RSI: {ta_profile.get('rsi_14')} | MACD: {'bullish' if ta_profile.get('macd_bullish') else 'bearish'}
- Support: ${ta_profile.get('support')} | Resistance: ${ta_profile.get('resistance')}

FLOW SIGNALS:
- {flow_signal.get('summary', 'N/A')}
- Options flow: {flow_signal.get('flow', {}).get('details', 'N/A')}
- Dark pool: {flow_signal.get('dark_pool', {}).get('details', 'N/A')}
- GEX wall: ${flow_signal.get('gex', {}).get('gamma_wall', 'N/A')}

WARNINGS: {rec.get('warnings', [])}
"""

    prompt = f"Explain this trade recommendation in 3-4 sentences:\n{facts}"
    return _call_ollama(prompt, system, max_tokens=300)


def summarize_signals(ticker: str, ta_profile: dict, flow_signal: dict) -> str:
    """
    Generate a brief signal summary — used in the daily 'run the analysis' framework.
    Highlights the 2-3 most important signals for this ticker today.
    """
    system = """You are a concise trading signal analyst.
Identify the 2-3 most significant signals from the data and explain them in 2-3 sentences.
Focus on what's actionable. Use exact numbers."""

    facts = f"""
TICKER: {ticker}
TA: {ta_profile.get('summary', 'N/A')}
RSI(14): {ta_profile.get('rsi_14')} | MACD bullish: {ta_profile.get('macd_bullish')}
Above MA200: {ta_profile.get('above_ma200')} | Trend: {ta_profile.get('trend')}
Support: ${ta_profile.get('support')} | Resistance: ${ta_profile.get('resistance')}
ATR: ${ta_profile.get('atr_14')}

OPTIONS FLOW: {flow_signal.get('summary', 'N/A')}
Flow direction: {flow_signal.get('flow', {}).get('direction')} ({flow_signal.get('flow', {}).get('score')}/100)
Dark pool: {flow_signal.get('dark_pool', {}).get('direction')} ({flow_signal.get('dark_pool', {}).get('score')}/100)
GEX wall: ${flow_signal.get('gex', {}).get('gamma_wall', 'N/A')}
Market tide: {flow_signal.get('market_tide', {}).get('direction')}
Earnings: {flow_signal.get('earnings_risk', {}).get('days_to_earnings')} days away
"""

    prompt = f"What are the 2-3 most important signals for {ticker} right now?\n{facts}"
    return _call_ollama(prompt, system, max_tokens=200)


def generate_sell_signal_narrative(ticker: str, position: dict, reason: str) -> str:
    """
    Generate plain-English narrative for a sell/exit signal.
    Called by Position Monitor (C13) when target/stop is hit.
    """
    system = "You are a trading assistant. In 1-2 sentences, explain why this position should be closed."
    prompt = f"""
Position: {ticker} {position.get('strategy', 'option')}
Entry: ${position.get('entry_price', 0)} | Current P&L: ${position.get('current_pnl', 0)} ({position.get('pnl_pct', 0)*100:.1f}%)
Exit reason: {reason}
DTE remaining: {position.get('dte_remaining', 'N/A')}
"""
    return _call_ollama(prompt, system, max_tokens=100)


def is_ollama_available() -> bool:
    """Check if Ollama is running and Qwen model is loaded."""
    try:
        import requests
        r = requests.get(f"{settings.ollama_host}/api/tags", timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        return any(settings.ollama_model.split(":")[0] in m for m in models)
    except Exception:
        return False