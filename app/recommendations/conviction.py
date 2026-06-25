"""
Phase A2 — Weighted Conviction Scoring (0-100).

Replaces binary 0-6 re-evaluation score with a weighted probability score
that reflects how likely a trade is to be profitable.

Weights are initialized from domain knowledge and recalibrated monthly
by the learning engine as real outcomes accumulate.

Conviction tiers:
    85-100: VERY_HIGH  → full position, act immediately
    70-84:  HIGH       → standard position, good entry
    55-69:  MODERATE   → small position, wait for trigger
    40-54:  WATCH      → monitor only, do not act
    <40:    SKIP       → filter out entirely
"""

# ─────────────────────────────────────────────────────────────────────────────
# Signal weights — sum to 100 base points
# Recalibrated monthly by learning engine
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_WEIGHTS = {
    "entry_trigger":  25,   # AT_RESISTANCE/AT_SUPPORT vs BETWEEN_LEVELS
    "volume":         20,   # rel_vol vs 20d avg
    "iv_rank":        15,   # IV cheap = good to buy, expensive = avoid
    "options_flow":   20,   # UW sweeps + dark pool confirming direction
    "vix_zone":       10,   # market environment
    "ta_alignment":   10,   # TA trend + MACD + RSI all agreeing
}

# LLM confidence modifier (applied on top of signal score)
LLM_CONFIDENCE_BONUS = {
    (80, 100): +10,
    (70,  79): +5,
    (60,  69): 0,
    (50,  59): -5,
    (0,   49): -15,
}

MIN_CONVICTION_TO_SURFACE = 70   # below this = don't show to consumer
TOP_N_PER_DAY             = 5    # max recommendations per user per day


def score_entry_trigger(price_ctx: dict, direction: str) -> tuple[float, str]:
    trigger = price_ctx.get("entry_trigger", "UNKNOWN")
    if direction == "BEARISH" and trigger in ("AT_RESISTANCE", "NEAR_RESISTANCE"):
        return 1.0, f"Perfect bearish entry: {trigger}"
    if direction == "BULLISH" and trigger in ("AT_SUPPORT", "NEAR_SUPPORT"):
        return 1.0, f"Perfect bullish entry: {trigger}"
    if trigger == "BETWEEN_LEVELS":
        return 0.6, "Between S/R levels — acceptable but not ideal"
    if direction == "BEARISH" and trigger in ("AT_SUPPORT", "NEAR_SUPPORT"):
        return 0.0, "Poor bearish entry: price at support — likely to bounce"
    if direction == "BULLISH" and trigger in ("AT_RESISTANCE", "NEAR_RESISTANCE"):
        return 0.0, "Poor bullish entry: price at resistance — likely to reject"
    return 0.5, f"Entry trigger: {trigger} (neutral)"


def score_volume(price_ctx: dict) -> tuple[float, str]:
    rel_vol   = price_ctx.get("relative_volume", 1.0) or 1.0
    confirmed = price_ctx.get("volume_confirmed", False)
    signal    = price_ctx.get("volume_signal", "UNKNOWN")
    if rel_vol >= 1.5:
        return 1.0, f"Strong volume: {rel_vol}x avg ({signal})"
    if rel_vol >= 1.0:
        return 0.8, f"Above average volume: {rel_vol}x avg"
    if rel_vol >= 0.7:
        return 0.5, f"Below average volume: {rel_vol}x avg — weaker signal"
    return 0.1, f"Very low volume: {rel_vol}x avg — signal unreliable"


def score_iv_rank(iv_ctx: dict, direction: str) -> tuple[float, str]:
    if iv_ctx.get("error"):
        return 0.5, "IV rank unavailable — neutral"
    iv_rank    = iv_ctx.get("iv_rank", 50) or 50
    buy_options = iv_ctx.get("buy_options", True)
    zone        = iv_ctx.get("iv_zone", "FAIR")
    # Buying options: want cheap IV (low rank)
    if buy_options:
        if iv_rank <= 20:
            return 1.0, f"IV very cheap (rank {iv_rank:.0f}) — great to buy"
        if iv_rank <= 40:
            return 0.8, f"IV cheap (rank {iv_rank:.0f}) — good to buy"
        if iv_rank <= 60:
            return 0.6, f"IV fair (rank {iv_rank:.0f}) — acceptable"
        if iv_rank <= 80:
            return 0.3, f"IV expensive (rank {iv_rank:.0f}) — consider spread"
        return 0.1, f"IV very expensive (rank {iv_rank:.0f}) — avoid buying"
    # Selling premium: want expensive IV
    else:
        if iv_rank >= 70:
            return 1.0, f"IV expensive (rank {iv_rank:.0f}) — great to sell"
        return 0.4, f"IV too cheap (rank {iv_rank:.0f}) — not enough premium"


def score_options_flow(signal_data: dict, direction: str) -> tuple[float, str]:
    flow_score = signal_data.get("flow_score", 0)
    dp_score   = signal_data.get("dp_score", 0)
    # Weekend/no data — neutral
    if flow_score == 0 and dp_score == 0:
        return 0.5, "No live flow data (weekend/holiday) — neutral"
    combined = (flow_score + dp_score) / 2
    if direction == "BEARISH":
        # High put flow = confirms bearish
        if combined >= 70:
            return 1.0, f"Strong bearish flow confirmed: dp={dp_score} flow={flow_score}"
        if combined >= 50:
            return 0.7, f"Moderate bearish flow: dp={dp_score} flow={flow_score}"
        return 0.3, f"Weak/contradicting flow: dp={dp_score} flow={flow_score}"
    else:
        if combined >= 70:
            return 1.0, f"Strong bullish flow confirmed: dp={dp_score} flow={flow_score}"
        if combined >= 50:
            return 0.7, f"Moderate bullish flow: dp={dp_score} flow={flow_score}"
        return 0.3, f"Weak/contradicting flow: dp={dp_score} flow={flow_score}"


def score_vix(vix_ctx: dict, direction: str) -> tuple[float, str]:
    zone  = vix_ctx.get("zone", "NORMAL")
    trend = vix_ctx.get("trend", "STABLE")
    current = vix_ctx.get("current", 17)
    if zone == "EXTREME":
        return 0.0, f"VIX EXTREME ({current}) — no new positions"
    if zone == "HIGH" and direction in ("BULLISH", "BEARISH"):
        return 0.2, f"VIX HIGH ({current}) — directional options risky"
    if zone == "LOW":
        return 1.0, f"VIX LOW ({current}) — great environment for buying options"
    if zone == "NORMAL":
        bonus = 0.0 if "RISING" in trend else 0.1
        return 0.8 + bonus, f"VIX NORMAL ({current}, {trend})"
    return 0.6, f"VIX ELEVATED ({current}) — prefer spreads"


def score_ta_alignment(ta_data: dict, direction: str) -> tuple[float, str]:
    signal   = ta_data.get("signal", "NEUTRAL")
    trend    = ta_data.get("trend", "SIDEWAYS")
    rsi      = ta_data.get("rsi_14", 50) or 50
    macd_sig = ta_data.get("macd_signal", "NEUTRAL")

    bearish_aligned = (
        signal in ("SELL", "STRONG_SELL") or
        trend == "DOWNTREND" and macd_sig == "BEARISH"
    )
    bullish_aligned = (
        signal in ("BUY", "STRONG_BUY") or
        trend == "UPTREND" and macd_sig == "BULLISH"
    )

    if direction == "BEARISH":
        if bearish_aligned and trend == "DOWNTREND":
            return 1.0, f"Full TA alignment: {trend}, {macd_sig}, RSI {rsi:.0f}"
        if bearish_aligned:
            return 0.7, f"Partial TA alignment: {signal}, {macd_sig}"
        if bullish_aligned:
            return 0.1, f"TA contradicts direction: {signal} in {trend}"
        return 0.4, f"TA neutral: {signal}, {trend}"
    elif direction == "BULLISH":
        if bullish_aligned and trend == "UPTREND":
            return 1.0, f"Full TA alignment: {trend}, {macd_sig}, RSI {rsi:.0f}"
        if bullish_aligned:
            return 0.7, f"Partial TA alignment: {signal}, {macd_sig}"
        if bearish_aligned:
            return 0.1, f"TA contradicts direction: {signal} in {trend}"
        return 0.4, f"TA neutral: {signal}, {trend}"
    return 0.3, f"Neutral direction with {signal} signal"


def get_llm_modifier(llm_confidence: int) -> int:
    for (low, high), bonus in LLM_CONFIDENCE_BONUS.items():
        if low <= llm_confidence <= high:
            return bonus
    return 0


def calculate_conviction(
    price_ctx:   dict,
    vix_ctx:     dict,
    iv_ctx:      dict,
    ta_data:     dict,
    signal_data: dict,
    direction:   str,
    llm_confidence: int = 50,
    weights:     dict | None = None,
) -> dict:
    """
    Calculate weighted conviction score 0-100 for a recommendation.

    Returns full breakdown for transparency and learning.
    """
    w = weights or DEFAULT_WEIGHTS

    # Score each criterion (0.0 to 1.0)
    scores = {
        "entry_trigger": score_entry_trigger(price_ctx, direction),
        "volume":         score_volume(price_ctx),
        "iv_rank":        score_iv_rank(iv_ctx, direction),
        "options_flow":   score_options_flow(signal_data, direction),
        "vix_zone":       score_vix(vix_ctx, direction),
        "ta_alignment":   score_ta_alignment(ta_data, direction),
    }

    # Weighted sum
    raw_score = sum(
        scores[k][0] * w.get(k, 0)
        for k in scores
    )

    # LLM confidence modifier
    llm_mod   = get_llm_modifier(llm_confidence)
    final     = max(0, min(100, round(raw_score + llm_mod)))

    # Conviction tier
    if final >= 85:
        tier = "VERY_HIGH"
        act_now = True
        size = "full"
    elif final >= 70:
        tier = "HIGH"
        act_now = True
        size = "standard"
    elif final >= 55:
        tier = "MODERATE"
        act_now = False
        size = "small — wait for entry trigger"
    elif final >= 40:
        tier = "WATCH"
        act_now = False
        size = "monitor only"
    else:
        tier = "SKIP"
        act_now = False
        size = "do not act"

    return {
        "conviction_score":     final,
        "conviction_tier":      tier,
        "act_now":              act_now,
        "position_size":        size,
        "llm_modifier":         llm_mod,
        "raw_score":            round(raw_score, 1),
        "breakdown": {
            k: {
                "score":    round(scores[k][0], 2),
                "weight":   w.get(k, 0),
                "points":   round(scores[k][0] * w.get(k, 0), 1),
                "note":     scores[k][1],
            }
            for k in scores
        },
        "passes_threshold":     final >= MIN_CONVICTION_TO_SURFACE,
    }


def get_learned_weights(user_id: str) -> dict:
    """
    Load calibrated weights from learning engine if available.
    Falls back to DEFAULT_WEIGHTS.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text("""
                SELECT conviction_weights FROM user_profiles
                WHERE user_id = :uid
            """), {"uid": user_id}).fetchone()
            if row and row.conviction_weights:
                import json
                return json.loads(row.conviction_weights)
    except Exception:
        pass
    return DEFAULT_WEIGHTS
