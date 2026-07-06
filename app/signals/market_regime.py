"""
Market Regime Signals — VIX Term Structure + Put/Call Ratio.

VIX Term Structure:
  ^VIX   = 30-day implied volatility (standard VIX)
  ^VIX9D = 9-day implied volatility (near-term fear)
  ^VIX3M = 3-month implied volatility
  
  Spread = VIX9D - VIX (negative = normal, positive = inverted = fear NOW)
  Ratio  = VIX9D / VIX (>1.0 = inverted term structure = caution)

Put/Call Ratio (UW total options volume):
  PCR = put_volume / call_volume
  PCR < 0.7  = extreme complacency = bearish contrarian
  PCR 0.7-0.9 = neutral/bullish
  PCR 0.9-1.1 = neutral
  PCR > 1.1  = elevated fear = bullish contrarian
  PCR > 1.3  = extreme fear = strong bullish contrarian
  
  PCR trend matters more than level:
    Rising 3 days = hedging building = caution
    Falling 3 days = complacency building = potential top
"""
import time
from datetime import datetime


def get_vix_term_structure() -> dict:
    """
    Fetch VIX term structure from yfinance.
    Returns spread, ratio, and interpretation.
    """
    try:
        import yfinance as yf

        vix9d = yf.Ticker("^VIX9D").fast_info.last_price or 0
        vix30 = yf.Ticker("^VIX").fast_info.last_price or 0
        vix3m = yf.Ticker("^VIX3M").fast_info.last_price or 0

        if not vix30:
            return {"error": "VIX data unavailable"}

        spread    = round(vix9d - vix30, 2)   # negative = normal
        ratio     = round(vix9d / vix30, 3) if vix30 else 1.0
        inverted  = ratio > 1.05              # near-term fear > long-term

        # Interpretation
        if inverted and ratio > 1.15:
            signal = "FEAR_SPIKE"
            bias   = "BEARISH"
            note   = f"VIX9D({vix9d:.1f}) >> VIX({vix30:.1f}) — near-term fear spiking, avoid buying"
        elif inverted:
            signal = "SLIGHT_INVERSION"
            bias   = "NEUTRAL"
            note   = f"Mild inversion VIX9D({vix9d:.1f}) > VIX({vix30:.1f}) — slight caution"
        elif spread < -3:
            signal = "STEEP_CONTANGO"
            bias   = "BULLISH"
            note   = f"Steep contango VIX9D({vix9d:.1f}) << VIX({vix30:.1f}) — calm short-term"
        else:
            signal = "NORMAL"
            bias   = "NEUTRAL"
            note   = f"Normal structure VIX9D({vix9d:.1f}) vs VIX({vix30:.1f})"

        return {
            "vix9d":    round(vix9d, 2),
            "vix30":    round(vix30, 2),
            "vix3m":    round(vix3m, 2),
            "spread":   spread,
            "ratio":    ratio,
            "inverted": inverted,
            "signal":   signal,
            "bias":     bias,
            "note":     note,
        }
    except Exception as e:
        return {"error": str(e), "vix30": 17, "signal": "UNKNOWN", "bias": "NEUTRAL"}


def get_put_call_ratio() -> dict:
    """
    Fetch market-wide put/call ratio from UW total options volume.
    Returns PCR, trend interpretation, and trading bias.
    """
    try:
        from app.options_flow.unusual_whales import _get

        data = _get("/api/market/total-options-volume")
        if not isinstance(data, list) or not data:
            return {"error": "No data", "pcr": 1.0, "signal": "NEUTRAL"}

        # Most recent day
        latest    = data[0]
        call_vol  = int(latest.get("call_volume", 0) or 0)
        put_vol   = int(latest.get("put_volume", 0)  or 0)
        call_prem = float(latest.get("call_premium", 0) or 0)
        put_prem  = float(latest.get("put_premium", 0)  or 0)

        pcr = round(put_vol / call_vol, 3) if call_vol else 1.0
        # Premium-weighted PCR (more accurate — large orders weighted more)
        pcr_prem = round(put_prem / call_prem, 3) if call_prem else 1.0

        # Interpretation (contrarian — high PCR = too many puts = market often reverses UP)
        if pcr < 0.65:
            signal = "EXTREME_COMPLACENCY"
            bias   = "BEARISH"
            note   = f"PCR {pcr:.2f} — too many calls, complacency high, watch for pullback"
        elif pcr < 0.75:
            signal = "COMPLACENCY"
            bias   = "SLIGHTLY_BEARISH"
            note   = f"PCR {pcr:.2f} — bullish bias but elevated call buying, some risk"
        elif pcr < 0.90:
            signal = "NEUTRAL_BULLISH"
            bias   = "BULLISH"
            note   = f"PCR {pcr:.2f} — healthy options market, slight bullish bias"
        elif pcr < 1.10:
            signal = "NEUTRAL"
            bias   = "NEUTRAL"
            note   = f"PCR {pcr:.2f} — balanced put/call activity"
        elif pcr < 1.25:
            signal = "ELEVATED_FEAR"
            bias   = "BULLISH"
            note   = f"PCR {pcr:.2f} — elevated hedging, contrarian bullish signal"
        else:
            signal = "EXTREME_FEAR"
            bias   = "STRONGLY_BULLISH"
            note   = f"PCR {pcr:.2f} — extreme put buying, strong contrarian bullish"

        return {
            "date":      latest.get("date", ""),
            "call_vol":  call_vol,
            "put_vol":   put_vol,
            "pcr":       pcr,
            "pcr_prem":  pcr_prem,
            "signal":    signal,
            "bias":      bias,
            "note":      note,
            "call_premium_B": round(call_prem / 1e9, 2),
            "put_premium_B":  round(put_prem / 1e9, 2),
        }
    except Exception as e:
        return {"error": str(e), "pcr": 1.0, "signal": "NEUTRAL", "bias": "NEUTRAL"}


def get_full_market_regime() -> dict:
    """
    Combined market regime signal from VIX term structure + PCR.
    Used to inform options strategy selection (direction, type).
    """
    ts  = get_vix_term_structure()
    pcr = get_put_call_ratio()

    # Combined bias
    bias_scores = {
        "STRONGLY_BULLISH": 2,
        "BULLISH":          1,
        "NEUTRAL_BULLISH":  0.5,
        "NEUTRAL":          0,
        "SLIGHTLY_BEARISH": -0.5,
        "BEARISH":          -1,
    }

    ts_score  = bias_scores.get(ts.get("bias","NEUTRAL"), 0)
    pcr_score = bias_scores.get(pcr.get("bias","NEUTRAL"), 0)

    # VIX term structure gets more weight (direct fear signal)
    combined = ts_score * 0.6 + pcr_score * 0.4

    if combined >= 1.2:
        overall_bias   = "STRONGLY_BULLISH"
        strategy_hint  = "Buy calls or call spreads. Elevated risk appetite."
    elif combined >= 0.4:
        overall_bias   = "BULLISH"
        strategy_hint  = "Favor call spreads or debit calls. Normal conditions."
    elif combined >= -0.4:
        overall_bias   = "NEUTRAL"
        strategy_hint  = "Iron condors or straddles. No clear directional edge."
    elif combined >= -1.0:
        overall_bias   = "BEARISH"
        strategy_hint  = "Favor put spreads. Elevated risk."
    else:
        overall_bias   = "STRONGLY_BEARISH"
        strategy_hint  = "Protective puts or cash. High fear environment."

    # VIX inversion overrides everything — near-term fear spike
    if ts.get("inverted") and ts.get("ratio", 1) > 1.10:
        overall_bias  = "CAUTION"
        strategy_hint = "VIX inverted — near-term fear spike. Reduce size or wait."

    return {
        "overall_bias":   overall_bias,
        "strategy_hint":  strategy_hint,
        "combined_score": round(combined, 2),
        "vix_structure":  ts,
        "put_call":       pcr,
        "summary": (
            f"VIX9D:{ts.get('vix9d',0):.1f} vs VIX:{ts.get('vix30',0):.1f} "
            f"({ts.get('signal','?')}) | "
            f"PCR:{pcr.get('pcr',0):.2f} ({pcr.get('signal','?')})"
        ),}
