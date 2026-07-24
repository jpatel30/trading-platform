"""
IV-Expansion Signal — LEADING indicator built on iv_history (Phase 2's
after-hours batch job populates one real row per watchlist ticker per
day, so a real, growing day-over-day series now exists for the first
time this session).

Same LEVEL vs VELOCITY distinction oi_flow.py already solved for open
interest, applied here to implied volatility: a single day's IV level
tells you nothing predictive on its own — what matters is whether IV
has been RISING over several consecutive days (institutions positioning
ahead of an expected move) vs flat/falling. The persistence multiplier
mirrors get_oi_buildup_signal()'s shape exactly: the magnitude of the
move is weighted up to 2x by how many of the most recent days each rose
(or fell) over the prior day — sustained multi-day movement is a
stronger signal than a single-day jump.

IV expansion is directionally NEUTRAL by itself — rising IV means the
market expects a BIGGER move, not which way. Signal labels reflect that
(EXPANDING/CONTRACTING, not BULLISH/BEARISH) — direction still comes
from flow/OI/technicals elsewhere in the pipeline.

Honest, expected result right now: most tickers will report
INSUFFICIENT_HISTORY (Phase 2 has only been running a few days) — that
is the correct result, not a bug. Never fabricate a score from 1-2 data
points.
"""

MIN_DAYS_FOR_SIGNAL = 5


def get_iv_expansion_signal(ticker: str) -> dict:
    """
    Reads the ticker's last 10 real iv_history rows (by recorded_at),
    computes the 5-day rate-of-change of atm_iv (same "+5%" convention
    already established in learning/weekly_review.py's
    IV_EXPANDING_THRESHOLD_PCT), and weights it by how many consecutive
    days atm_iv has been moving the same direction.

    Returns:
        {
            "score": float in [-100, 100], or None if insufficient history
            "signal": one of STRONG_EXPANDING_IV / EXPANDING_IV / FLAT_IV /
                       CONTRACTING_IV / STRONG_CONTRACTING_IV /
                       INSUFFICIENT_HISTORY / NO_DATA / ERROR
            "days_of_data": int, real row count found (<=10)
            "insufficient_history": bool
            "current_iv": float or None — most recent atm_iv, present
                          even when there isn't enough history for a score
            "raw_change_pct": float or None — the raw 5-day rate of change
            "consecutive_days": int — how many of the most recent days
                                 each moved the same direction as raw_change_pct
            "raw_5day_values": list[float] — the actual values used
        }
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT recorded_at, atm_iv FROM iv_history
                WHERE ticker = :t AND atm_iv IS NOT NULL
                ORDER BY recorded_at DESC LIMIT 10
            """), {"t": ticker.upper()}).fetchall()
    except Exception as e:
        return {"score": None, "signal": "ERROR", "days_of_data": 0,
                "insufficient_history": True, "current_iv": None,
                "raw_change_pct": None, "consecutive_days": 0,
                "raw_5day_values": [], "error": str(e)}

    if not rows:
        return {"score": None, "signal": "NO_DATA", "days_of_data": 0,
                "insufficient_history": True, "current_iv": None,
                "raw_change_pct": None, "consecutive_days": 0, "raw_5day_values": []}

    # DB order is newest-first; reverse to chronological (oldest first) —
    # matches oi_flow.py-style day-over-day reasoning.
    values       = [float(r.atm_iv) for r in reversed(rows)]
    days_of_data = len(values)
    current_iv   = values[-1]

    if days_of_data < MIN_DAYS_FOR_SIGNAL:
        return {"score": None, "signal": "INSUFFICIENT_HISTORY", "days_of_data": days_of_data,
                "insufficient_history": True, "current_iv": current_iv,
                "raw_change_pct": None, "consecutive_days": 0, "raw_5day_values": values}

    # Headline rate-of-change over the most recent 5 days, even when more
    # history exists (keeps this consistent with weekly_review.py's own
    # "5-day trend" convention) — persistence below still looks at the
    # full available window (up to 10 days).
    last5  = values[-5:]
    oldest = last5[0]
    raw_change_pct = round((current_iv - oldest) / oldest * 100, 2) if oldest else 0.0

    # Persistence — how many of the most recent days each moved the same
    # direction as the overall trend, counting back from today. Same
    # shape as oi_flow.py's days_of_oi_increases: sustained multi-day
    # movement is weighted up to 2x, a single-day blip isn't.
    consecutive = 0
    rising = raw_change_pct >= 0
    for i in range(len(values) - 1, 0, -1):
        day_rose = values[i] > values[i - 1]
        if day_rose == rising and values[i] != values[i - 1]:
            consecutive += 1
        else:
            break

    persistence_mult = 1.0 + min(consecutive, 10) / 10.0
    score = max(-100.0, min(100.0, round(raw_change_pct * persistence_mult, 1)))

    if   score >  40: signal = "STRONG_EXPANDING_IV"
    elif score >  15: signal = "EXPANDING_IV"
    elif score < -40: signal = "STRONG_CONTRACTING_IV"
    elif score < -15: signal = "CONTRACTING_IV"
    else:             signal = "FLAT_IV"

    return {
        "score": score,
        "signal": signal,
        "days_of_data": days_of_data,
        "insufficient_history": False,
        "current_iv": current_iv,
        "raw_change_pct": raw_change_pct,
        "consecutive_days": consecutive,
        "raw_5day_values": last5,
    }
