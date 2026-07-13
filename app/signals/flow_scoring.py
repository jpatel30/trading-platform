"""
Shared flow + dark pool scoring — single source of truth.

Historical bug (fixed here once, for all callers): earlier code checked
alert.get("sentiment") for BULLISH/BEARISH, but UW's flow_alerts payload
carries no "sentiment" field at all — it's always "". The real signal is
the "type" field ("call"/"put"). This exact bug was independently
reimplemented, and independently broken, in FIVE separate places:
quick_scan.py, rescan_engine.py (two locations — the top-level batch
AND a nested per-ticker rotation helper), smart_engine.py,
velocity_tracker.py (two locations), and smart_stock_scan.py.

Dark pool prints have the same problem: "side" is always "". Real
direction is reconstructed from price vs NBBO — a print at/above the
ask is an aggressive buy, at/below the bid is an aggressive sell.

Every caller should import from here rather than reimplementing this.
"""


def classify_flow_type(alert: dict) -> str | None:
    """Return 'CALL' or 'PUT' for an options-flow alert, or None if unknown."""
    t = (alert.get("type") or "").strip().lower()
    if t == "call":
        return "CALL"
    if t == "put":
        return "PUT"
    s = (alert.get("sentiment") or "").strip().upper()  # legacy fallback
    if s in ("BULLISH", "CALL"):
        return "CALL"
    if s in ("BEARISH", "PUT"):
        return "PUT"
    return None


def compute_flow_score(alerts: list[dict]) -> dict:
    """Aggregate flow alerts into a -100..+100 directional score."""
    bull = bear = sweeps = 0
    call_vol = put_vol = 0
    for a in alerts or []:
        kind = classify_flow_type(a)
        vol  = a.get("volume", 0) or 0
        if kind == "CALL":
            bull += 1
            call_vol += vol
        elif kind == "PUT":
            bear += 1
            put_vol += vol
        if a.get("is_sweep"):
            sweeps += 1

    total = bull + bear
    score = round((bull - bear) / total * 100, 1) if total else 0.0
    return {
        "flow_score":  score,
        "alert_count": total,
        "sweep_count": sweeps,
        "call_vol":    call_vol,
        "put_vol":     put_vol,
        "call_put_ratio": round(call_vol / put_vol, 2) if put_vol else None,
    }


def classify_dp_side(dp_print: dict) -> str | None:
    """Return 'BUY' or 'SELL' for a dark pool print, from price vs NBBO."""
    try:
        price = float(dp_print.get("price", 0) or 0)
        ask   = float(dp_print.get("nbbo_ask", 0) or 0)
        bid   = float(dp_print.get("nbbo_bid", 0) or 0)
        if ask and price >= ask * 0.999:
            return "BUY"
        if bid and price <= bid * 1.001:
            return "SELL"
    except (TypeError, ValueError):
        pass
    side = (dp_print.get("side") or "").strip().upper()  # legacy fallback
    if side in ("BUY", "A"):
        return "BUY"
    if side in ("SELL", "B"):
        return "SELL"
    return None


def compute_dp_score(prints: list[dict]) -> dict:
    """Aggregate dark pool prints into a -100..+100 directional score."""
    buys = sells = 0
    for d in prints or []:
        side = classify_dp_side(d)
        if side == "BUY":
            buys += 1
        elif side == "SELL":
            sells += 1
    total = buys + sells
    score = round((buys - sells) / total * 100, 1) if total else 0.0
    return {"dp_score": score, "dp_prints": total, "dp_buy": buys, "dp_sell": sells}


def combined_direction(flow_score: float, dp_score: float) -> str:
    """Blended flow+dp direction label — only for places needing one string."""
    total = flow_score + dp_score
    if total > 0:
        return "BULLISH"
    if total < 0:
        return "BEARISH"
    return "NEUTRAL"
