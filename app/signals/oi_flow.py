"""
Open Interest Buildup Signal — LEADING indicator.

Unlike flow alerts (reactive — shows trades happening NOW), OI change
confirms NEW positions being opened over time. days_of_oi_increases shows
sustained multi-day accumulation, which is the closest signal UW offers
to "institutions positioning before a catalyst."

Logic:
  CALL + aggressive ask-side buying + OI increasing = bullish accumulation
  PUT  + aggressive ask-side buying + OI increasing = bearish accumulation
  CALL + bid-side selling (OI still up) = call writing, weak bearish
  PUT  + bid-side selling (OI still up) = cash-secured puts, weak bullish
  Persistent days_of_oi_increases = stronger conviction than one-day noise
"""


def _parse_option_type(symbol: str) -> str | None:
    """Parse CALL/PUT from OCC symbol (e.g. AAPL260710C00325000)."""
    for marker, label in (("C", "CALL"), ("P", "PUT")):
        idx = symbol.rfind(marker)
        if idx > 0 and symbol[idx+1:].isdigit() and len(symbol[idx+1:]) == 8:
            return label
    return None


def get_oi_buildup_signal(ticker: str) -> dict:
    """
    Analyze OI change data to detect institutional pre-move accumulation.
    Returns score (-100 to +100), signal label, and persistence context.
    """
    try:
        from app.options_flow.unusual_whales import _get
        data = _get(f"/api/stock/{ticker}/oi-change")
        contracts = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])

        if not contracts:
            return {"score": 0, "signal": "NO_DATA", "max_days_building": 0,
                    "top_contract": "", "top_oi_diff": 0}

        bullish_weight = 0.0
        bearish_weight = 0.0
        max_days      = 0
        top_contract  = ""
        top_diff      = 0

        for c in contracts[:30]:
            sym = c.get("option_symbol", "")
            opt_type = _parse_option_type(sym)
            if not opt_type:
                continue

            oi_diff = float(c.get("oi_diff_plain", 0) or 0)
            if oi_diff <= 0:
                continue  # only count OI BUILDING, not unwinding

            ask_v = float(c.get("prev_ask_volume", 0) or 0)
            bid_v = float(c.get("prev_bid_volume", 0) or 0)
            tot_v = ask_v + bid_v
            if tot_v == 0:
                continue

            days_building = int(c.get("days_of_oi_increases", 0) or 0)
            ask_ratio      = ask_v / tot_v  # >0.5 = aggressive buying

            # Persistence multiplier — up to 2x weight for 10+ day builds
            persistence_mult = 1.0 + min(days_building, 10) / 10.0
            weight = oi_diff * persistence_mult

            if opt_type == "CALL" and ask_ratio > 0.5:
                bullish_weight += weight
            elif opt_type == "PUT" and ask_ratio > 0.5:
                bearish_weight += weight
            elif opt_type == "CALL":
                bearish_weight += weight * 0.3   # call writing, weak signal
            elif opt_type == "PUT":
                bullish_weight += weight * 0.3   # cash-secured puts, weak signal

            if days_building > max_days:
                max_days = days_building
            if oi_diff > top_diff:
                top_diff, top_contract = oi_diff, sym

        total = bullish_weight + bearish_weight
        score = round((bullish_weight - bearish_weight) / total * 100, 1) if total else 0

        if   score >  40: signal = "STRONG_BULLISH_BUILDUP"
        elif score >  15: signal = "BULLISH_BUILDUP"
        elif score < -40: signal = "STRONG_BEARISH_BUILDUP"
        elif score < -15: signal = "BEARISH_BUILDUP"
        else:             signal = "NEUTRAL"

        return {
            "score":             score,
            "signal":            signal,
            "max_days_building": max_days,
            "top_contract":      top_contract,
            "top_oi_diff":       int(top_diff),
        }
    except Exception as e:
        return {"score": 0, "signal": "ERROR", "max_days_building": 0,
                "top_contract": "", "top_oi_diff": 0, "error": str(e)}
