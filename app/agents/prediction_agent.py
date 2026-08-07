"""
Prediction Agent — Routing Phase (MULTIAGENT_MIGRATION.md item 7).

Deterministic, pre-LLM pass: for an already-enriched candidate (same
shape smart_engine.py::_enrich_ticker produces), compute a direction
lean, rough strategy shape, and rough strikes - using the existing
signal/math layers named in the doc (flow_scoring, oi_flow,
market_regime, iv_expansion, technical_analysis, the R/R and EV gates),
not new logic reinvented from scratch:

  - direction lean: a convergence vote across flow/dp/OI/TA/insider/
    congress/institutional-ownership/market-regime, same "count bull vs
    bear across strong signals" pattern quick_scan.py already uses for
    its lighter pre-enrichment signal set, extended here with what's
    only available after _enrich_ticker() runs.
  - strategy shape + rough strikes: reuses strategy/engine.py's
    _deterministic_strategy() as-is - it already IS this exact rule
    (IV level -> spread/naked/condor/straddle shape, percentage-based
    OTM strikes), already proven as the system's own LLM-unavailable
    fallback. Not duplicated, imported.
  - R/R gate: a genuinely rough, live-quote-free version - BSM prices
    each leg (no UW call, so this scales to every scanned candidate,
    not just the few reaching a real LLM call) and applies the SAME
    DTE-scaled R/R floor _execute_trade_math uses, so "clears the rough
    gate" means something real, not just a signal-count heuristic. This
    is deliberately NOT the same call as the final gate applied later
    (items 18-19, Prediction Agent's Finalize Phase) - that one uses
    real UW quotes on Bull/Bear's actual output. This is a cheap first
    filter, not a substitute for it.

Items 8-10 (candidate_directions / tie_break_queue tables and the
classification logic that writes into them) are NOT built here - this
module is the scoring function those tables will store the output of,
not the storage/routing decision itself.
"""
from datetime import datetime, timedelta

SPREAD_WIDTH = 10.0   # matches strategy/engine.py::_deterministic_strategy exactly
IV_HIGH      = 0.40   # matches strategy/engine.py::_deterministic_strategy exactly


def _direction_votes(enriched: dict, market_regime: dict | None) -> tuple[str, int, list[str]]:
    """
    Rule-based direction lean. Same convergence-vote shape as
    quick_scan.py's Signal 1-6 (count bull vs bear across signals that
    cleared their own materiality threshold), extended with signals
    only available post-enrichment: OI buildup, insider activity,
    congress trades, institutional ownership, and market-wide regime.
    IV expansion is deliberately NOT a vote here, same reason
    quick_scan.py excludes it: it's directionally neutral by design
    (bigger expected move, not which way).

    Votes are structured ({signal, direction, detail}), not just flat
    strings - item 9 (tie_break_queue) needs to record WHICH signals
    actually disagreed, not just that the vote was ambiguous.
    """
    votes = _structured_votes(enriched, market_regime)
    direction, confidence = _classify_votes(votes)
    return direction, confidence, [v["detail"] for v in votes]


def _classify_votes(votes: list[dict]) -> tuple[str, int]:
    """Bull-vs-bear majority vote -> (direction, confidence_of_routing).
    A true tie (bull == bear, both >=1) is NEUTRAL - genuinely mixed,
    not defaulted either way. Shared by _direction_votes and
    route_candidate so there's one counting rule, not two copies."""
    bull  = sum(1 for v in votes if v["direction"] == "BULLISH")
    bear  = sum(1 for v in votes if v["direction"] == "BEARISH")
    total = len(votes)
    if total == 0:
        return "NEUTRAL", 0
    if bull > bear:
        return "BULLISH", round(bull / total * 100)
    if bear > bull:
        return "BEARISH", round(bear / total * 100)
    return "NEUTRAL", 50


def _structured_votes(enriched: dict, market_regime: dict | None) -> list[dict]:
    """Same signals/thresholds as _direction_votes, structured per-vote
    ({signal, direction, detail}) so a genuine tie can be traced back to
    exactly which signals disagreed (item 9's which_rule_conflict_triggered)."""
    votes = []

    flow = enriched.get("flow_score", 0)
    if abs(flow) >= 10:
        votes.append({"signal": "flow", "direction": "BULLISH" if flow > 0 else "BEARISH",
                       "detail": f"flow{flow:+.0f}"})

    dp = enriched.get("dp_score", 0)
    if abs(dp) >= 10:
        votes.append({"signal": "dark_pool", "direction": "BULLISH" if dp > 0 else "BEARISH",
                       "detail": f"dp{dp:+.0f}"})

    oi_score = enriched.get("oi_score", 0)
    oi_days  = enriched.get("oi_max_days", 0)
    if abs(oi_score) >= 40 and oi_days >= 5:
        votes.append({"signal": "oi_buildup", "direction": "BULLISH" if oi_score > 0 else "BEARISH",
                       "detail": f"oi{oi_score:+.0f}/{oi_days}d"})

    trend = enriched.get("trend", "SIDEWAYS")
    macd  = enriched.get("macd", "NEUTRAL")
    if trend == "UPTREND" and macd == "BULLISH_CROSS":
        votes.append({"signal": "ta", "direction": "BULLISH", "detail": "ta_bullish"})
    elif trend == "DOWNTREND" and macd == "BEARISH_CROSS":
        votes.append({"signal": "ta", "direction": "BEARISH", "detail": "ta_bearish"})

    insider = enriched.get("insider_signal", "NEUTRAL")
    if insider in ("STRONG_BUY", "BULLISH"):
        votes.append({"signal": "insider", "direction": "BULLISH", "detail": f"insider:{insider}"})
    elif insider in ("STRONG_SELL", "BEARISH"):
        votes.append({"signal": "insider", "direction": "BEARISH", "detail": f"insider:{insider}"})

    buys  = enriched.get("congress_buys", 0)
    sells = enriched.get("congress_sells", 0)
    if buys + sells >= 5:
        votes.append({"signal": "congress", "direction": "BULLISH" if buys > sells else "BEARISH",
                       "detail": f"congress{buys}b/{sells}s"})

    inst = enriched.get("inst_own_score", 50)
    if inst >= 80:
        votes.append({"signal": "institutional_ownership", "direction": "BULLISH", "detail": f"inst_own{inst}"})
    elif inst <= 20:
        votes.append({"signal": "institutional_ownership", "direction": "BEARISH", "detail": f"inst_own{inst}"})

    if market_regime:
        bias = market_regime.get("overall_bias", "NEUTRAL")
        if bias in ("STRONGLY_BULLISH", "BULLISH"):
            votes.append({"signal": "market_regime", "direction": "BULLISH", "detail": f"regime:{bias}"})
        elif bias in ("STRONGLY_BEARISH", "BEARISH"):
            votes.append({"signal": "market_regime", "direction": "BEARISH", "detail": f"regime:{bias}"})

    return votes


def _rough_rr_check(strategy: str, legs: list[dict], spot: float, dte: int, avg_iv: float) -> tuple[bool, float]:
    """
    Live-quote-free R/R sanity check on _deterministic_strategy's rough
    strikes: BSM-price each leg, compute rough entry/max-profit/max-loss
    using the SAME width-based formulas _execute_trade_math uses for
    debit vs credit strategies, then apply the SAME DTE-scaled R/R floor
    (0.5 base, loosening with DTE, flat 0.15 for credit strategies -
    strategy/engine.py's own established thresholds, not new numbers).
    """
    try:
        from py_vollib.black_scholes import black_scholes
    except Exception:
        return True, 0.0   # can't evaluate — don't block routing on a missing dep

    is_credit = strategy in ("CREDIT_CALL_SPREAD", "CREDIT_PUT_SPREAD", "IRON_CONDOR")
    t = max(dte, 1) / 365.0

    try:
        priced = []
        for leg in legs:
            flag  = "c" if leg["type"] == "CALL" else "p"
            price = max(0.01, float(black_scholes(flag, spot, leg["strike"], t, 0.05, avg_iv)))
            priced.append((leg["action"], price))

        buy_total  = sum(p for a, p in priced if a == "BUY")
        sell_total = sum(p for a, p in priced if a == "SELL")

        if is_credit:
            credit = sell_total - buy_total
            if credit <= 0:
                return False, 0.0
            max_profit = credit
            max_loss   = max(0.01, SPREAD_WIDTH - credit) if len(legs) > 1 else spot
        else:
            debit = buy_total - sell_total
            if debit <= 0:
                return False, 0.0
            max_loss = debit
            max_profit = max(0.01, SPREAD_WIDTH - debit) if len(legs) > 1 else spot * 0.20

        rr = round(max_profit / max_loss, 4) if max_loss > 0 else 0.0

        rr_min = 0.5
        if dte >= 60:   rr_min = 0.20
        elif dte >= 30: rr_min = 0.30
        elif dte >= 14: rr_min = 0.40
        rr_min_check = 0.15 if is_credit else rr_min

        return rr >= rr_min_check, rr
    except Exception:
        return True, 0.0   # BSM failure (bad inputs) — don't block routing on a math error


def route_candidate(enriched: dict, market_regime: dict | None = None) -> dict:
    """
    Full routing pass for one enriched candidate. Returns:
      ticker, direction_lean, strategy_shape, rough_strikes (legs),
      expiry, confidence_of_routing (0-100, signal-vote based),
      clears_rough_rr_gate, rough_rr, signal_reasons, signal_votes
      (structured - see _structured_votes)
    """
    ticker = enriched.get("ticker", "")
    spot   = float(enriched.get("price", 0) or 0)
    avg_iv = float(enriched.get("iv_current", 30)) / 100.0
    dte    = 21   # rough routing horizon — Finalize Phase picks the real expiry later

    votes   = _structured_votes(enriched, market_regime)
    reasons = [v["detail"] for v in votes]
    direction, confidence = _classify_votes(votes)

    if spot <= 0:
        return {
            "ticker": ticker, "direction_lean": direction, "strategy_shape": None,
            "rough_strikes": [], "expiry": None, "confidence_of_routing": confidence,
            "clears_rough_rr_gate": False, "rough_rr": 0.0,
            "signal_reasons": reasons, "signal_votes": votes,
            "error": "no live price",
        }

    from app.strategy.engine import _deterministic_strategy
    shape = _deterministic_strategy(
        direction if direction != "NEUTRAL" else "NEUTRAL",
        confidence, avg_iv, spot, dte,
        call_strikes=[], put_strikes=[], atr=0,   # confirmed unused inside the function
    )

    clears_gate, rough_rr = _rough_rr_check(shape["strategy"], shape["legs"], spot, dte, avg_iv)

    return {
        "ticker":                ticker,
        "direction_lean":        direction,
        "strategy_shape":        shape["strategy"],
        "rough_strikes":         shape["legs"],
        "expiry":                shape["expiry"],
        "confidence_of_routing": confidence,
        "clears_rough_rr_gate":  clears_gate,
        "rough_rr":              rough_rr,
        "signal_reasons":        reasons,
        "signal_votes":          votes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Classification + storage (items 8-10)
# ─────────────────────────────────────────────────────────────────────────────
#
# candidate_directions: every routed candidate's output (item 8).
# tie_break_queue: only the genuinely-mixed ones - a real tie in the
#   vote (bull == bear, both >=1), not just a narrow majority. A
#   majority (even 2-vs-1) is a real directional conclusion Bull or
#   Bear can argue FROM; a true tie has no majority to hand to either
#   one alone, which is exactly what item 10 means by "clear" vs
#   "genuinely mixed" - it's the same NEUTRAL result _classify_votes
#   already produces, not a second, separately-tuned threshold.

def _classify_routing(direction: str) -> str:
    """item 10: clear bullish -> BULL_ONLY, clear bearish -> BEAR_ONLY,
    genuinely mixed (NEUTRAL - a true tie in the vote) -> TIE_BREAK."""
    if direction == "BULLISH":
        return "BULL_ONLY"
    if direction == "BEARISH":
        return "BEAR_ONLY"
    return "TIE_BREAK"


def _conflict_description(votes: list[dict]) -> str:
    """which_rule_conflict_triggered: names the actual signals that
    disagreed, not just 'ambiguous'. e.g. 'flow=BULLISH vs insider=BEARISH'."""
    bulls = [v["signal"] for v in votes if v["direction"] == "BULLISH"]
    bears = [v["signal"] for v in votes if v["direction"] == "BEARISH"]
    if not bulls and not bears:
        return "no signals fired — nothing to route on"
    return f"{'+'.join(bulls) or 'none'}=BULLISH vs {'+'.join(bears) or 'none'}=BEARISH"


def classify_and_store(user_id: str, enriched: dict, market_regime: dict | None = None) -> dict:
    """
    Routes one enriched candidate (item 7) and stores it (item 8),
    queuing genuinely mixed cases for tie-break (items 9-10).
    """
    from sqlalchemy import text
    from app.db.session import get_session

    route = route_candidate(enriched, market_regime)
    classification = _classify_routing(route["direction_lean"])
    route["routing_classification"] = classification

    import json
    with get_session() as db:
        row = db.execute(text("""
            INSERT INTO candidate_directions (
                user_id, ticker, direction_lean, confidence_of_routing,
                strategy_shape, rough_strikes, expiry, clears_rough_rr_gate,
                rough_rr, signal_votes, routing_classification
            ) VALUES (
                :uid, :ticker, :direction, :confidence,
                :strategy, CAST(:strikes AS jsonb), :expiry, :clears_gate,
                :rough_rr, CAST(:votes AS jsonb), :classification
            )
            RETURNING id
        """), {
            "uid": user_id, "ticker": route["ticker"], "direction": route["direction_lean"],
            "confidence": route["confidence_of_routing"], "strategy": route["strategy_shape"],
            "strikes": json.dumps(route["rough_strikes"]), "expiry": route["expiry"],
            "clears_gate": route["clears_rough_rr_gate"], "rough_rr": route["rough_rr"],
            "votes": json.dumps(route["signal_votes"]), "classification": classification,
        }).fetchone()
        candidate_direction_id = str(row.id)

        if classification == "TIE_BREAK":
            bullish = [v for v in route["signal_votes"] if v["direction"] == "BULLISH"]
            bearish = [v for v in route["signal_votes"] if v["direction"] == "BEARISH"]
            db.execute(text("""
                INSERT INTO tie_break_queue (
                    candidate_direction_id, user_id, ticker,
                    which_rule_conflict_triggered, bullish_signals, bearish_signals
                ) VALUES (
                    :cdid, :uid, :ticker, :conflict,
                    CAST(:bulls AS jsonb), CAST(:bears AS jsonb)
                )
            """), {
                "cdid": candidate_direction_id, "uid": user_id, "ticker": route["ticker"],
                "conflict": _conflict_description(route["signal_votes"]),
                "bulls": json.dumps(bullish), "bears": json.dumps(bearish),
            })

    route["candidate_direction_id"] = candidate_direction_id
    return route
