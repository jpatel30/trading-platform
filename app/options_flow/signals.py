"""
Options Flow Signal Scorer (Component C4 — signals layer).

Takes raw data from UnusualWhalesClient and computes a structured
signal summary — direction, confidence, key signals — ready for
the Strategy Engine (Component C7).

Signal architecture:
    1. Options Flow Score   — sweep activity, ask-side premium, sweep count
    2. Dark Pool Score      — institutional block trade size and direction
    3. GEX Score            — dealer positioning, gamma wall, neg/pos GEX
    4. Market Tide Score    — net market call/put premium flow
    5. Earnings Risk Score  — proximity to earnings (Rule 2 gate)
    Combined → direction (BULLISH/BEARISH/NEUTRAL) + confidence (0-100)

Usage:
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package

    pkg = get_signal_package('NVDA')
    scored = score_signal_package(pkg)
    print(scored['summary'])
"""
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _days_to_earnings(earnings_history: list[dict]) -> int | None:
    """Return days until the next scheduled earnings report, or None."""
    today = datetime.utcnow().date()
    future_dates = []
    for e in earnings_history:
        rd = e.get("report_date")
        if not rd:
            continue
        try:
            d = datetime.strptime(str(rd)[:10], "%Y-%m-%d").date()
            if d >= today:
                future_dates.append(d)
        except ValueError:
            continue
    if not future_dates:
        return None
    return (min(future_dates) - today).days


# ─────────────────────────────────────────────────────────────────────────────
# Individual Signal Scorers
# ─────────────────────────────────────────────────────────────────────────────

def score_flow_alerts(alerts: list[dict]) -> dict:
    """
    Score options flow from flow_alerts data.

    Signals:
    - Large total_premium ($500K+ per alert) = institutional
    - has_sweep = True = aggressive multi-exchange buyer
    - total_ask_side_prem >> total_bid_side_prem = aggressive buyer
    - type (call/put) determines direction
    """
    if not alerts:
        return {"score": 50, "direction": "NEUTRAL", "details": "No flow alerts for this ticker today"}

    call_prem = 0.0
    put_prem  = 0.0
    call_sweeps = 0
    put_sweeps  = 0
    total_sweep_prem = 0.0

    for a in alerts:
        prem       = _safe_float(a.get("total_premium"))
        ask_prem   = _safe_float(a.get("total_ask_side_prem"))
        bid_prem   = _safe_float(a.get("total_bid_side_prem"))
        is_sweep   = bool(a.get("has_sweep"))
        alert_type = str(a.get("type", "")).lower()
        aggressive = ask_prem > bid_prem  # buyer hitting the ask = aggressive

        if alert_type == "call":
            call_prem += prem
            if is_sweep and aggressive:
                call_sweeps  += 1
                total_sweep_prem += prem
        elif alert_type == "put":
            put_prem += prem
            if is_sweep and aggressive:
                put_sweeps  += 1
                total_sweep_prem += prem

    total_prem = call_prem + put_prem
    if total_prem == 0:
        return {"score": 0, "direction": "NEUTRAL", "details": "No premium data"}

    call_pct = call_prem / total_prem

    # Score: 0=extreme bearish, 50=neutral, 100=extreme bullish
    score = 50 + (call_pct - 0.5) * 80   # ±40 from call/put split
    score += min(call_sweeps * 8, 20)      # up to +20 for call sweeps
    score -= min(put_sweeps  * 8, 20)      # up to -20 for put sweeps
    score = max(0, min(100, round(score)))

    direction = "BULLISH" if score > 60 else ("BEARISH" if score < 40 else "NEUTRAL")
    details = (
        f"Call ${call_prem/1e6:.1f}M vs Put ${put_prem/1e6:.1f}M | "
        f"Call sweeps: {call_sweeps} | Put sweeps: {put_sweeps} | "
        f"Total sweep prem: ${total_sweep_prem/1e6:.1f}M"
    )

    return {
        "score": score,
        "direction": direction,
        "call_premium": call_prem,
        "put_premium": put_prem,
        "call_sweeps": call_sweeps,
        "put_sweeps": put_sweeps,
        "total_sweep_premium": total_sweep_prem,
        "details": details,
    }


def score_dark_pool(dark_pool: list[dict]) -> dict:
    """
    Score dark pool activity for a ticker.

    Signals:
    - Large premium ($2M+) = institutional block
    - price vs nbbo_ask: below ask = potential accumulation
    - price vs nbbo_bid: above bid = potential distribution
    """
    if not dark_pool:
        return {"score": 50, "direction": "NEUTRAL", "details": "No dark pool data"}

    total_premium = 0.0
    above_ask_prem = 0.0   # buying above ask = aggressive buyer
    below_bid_prem = 0.0   # selling below bid = aggressive seller

    for t in dark_pool:
        prem = _safe_float(t.get("premium"))
        price = _safe_float(t.get("price"))
        ask   = _safe_float(t.get("nbbo_ask"))
        bid   = _safe_float(t.get("nbbo_bid"))

        total_premium += prem
        if ask > 0 and price >= ask:
            above_ask_prem += prem
        elif bid > 0 and price <= bid:
            below_bid_prem += prem

    if total_premium == 0:
        return {"score": 50, "direction": "NEUTRAL", "details": "No premium"}

    buy_pct  = above_ask_prem / total_premium
    sell_pct = below_bid_prem / total_premium

    # If neither aggressive buy nor sell detected, default to NEUTRAL (50)
    if above_ask_prem == 0 and below_bid_prem == 0:
        score = 50
    else:
        score = 50 + (buy_pct - sell_pct) * 50
    score = max(0, min(100, round(score)))

    direction = "BULLISH" if score > 60 else ("BEARISH" if score < 40 else "NEUTRAL")
    details = (
        f"Total DP: ${total_premium/1e6:.1f}M | "
        f"Above ask: ${above_ask_prem/1e6:.1f}M | "
        f"Below bid: ${below_bid_prem/1e6:.1f}M"
    )

    return {
        "score": score,
        "direction": direction,
        "total_premium": total_premium,
        "aggressive_buy_premium": above_ask_prem,
        "aggressive_sell_premium": below_bid_prem,
        "details": details,
    }


def score_gex(gex: list[dict], gex_by_strike: list[dict], current_price: float = 0) -> dict:
    """
    Score GEX (dealer gamma exposure).

    Signals:
    - Net gamma (call_gamma - put_gamma) > 0 = stabilizing
    - Net gamma < 0 = volatile, moves amplify (bearish risk)
    - Gamma wall: strike with highest net GEX = gravitational level
    - Negative GEX strikes below price = downside accelerator
    """
    if not gex:
        return {"score": 50, "direction": "NEUTRAL", "details": "No GEX data"}

    latest = gex[-1] if gex else {}
    call_gamma = _safe_float(latest.get("call_gamma"))
    put_gamma  = _safe_float(latest.get("put_gamma"))
    net_gamma  = call_gamma + put_gamma  # put_gamma is typically negative

    # Find gamma wall (strike with highest net |GEX|)
    gamma_wall = None
    max_gex = 0
    if gex_by_strike and current_price > 0:
        for s in gex_by_strike:
            net = _safe_float(s.get("call_gex")) + _safe_float(s.get("put_gex"))
            if abs(net) > max_gex:
                max_gex = abs(net)
                gamma_wall = _safe_float(s.get("strike"))

    # Score: positive GEX = stabilizing = slightly bullish (mean-reverting)
    #        negative GEX = volatile = amplified moves (risky)
    if net_gamma > 0:
        score = 55   # slightly bullish (market is pinned, sellers of options win)
    elif net_gamma < -1e9:
        score = 35   # strongly negative GEX = big moves expected = bearish risk
    else:
        score = 45

    direction = "NEUTRAL"
    if gamma_wall and current_price > 0:
        if gamma_wall > current_price * 1.02:
            direction = "BULLISH"   # gamma wall above = price pulled up
        elif gamma_wall < current_price * 0.98:
            direction = "BEARISH"   # gamma wall below = price pulled down

    details = (
        f"Net gamma: {net_gamma:.2e} | "
        f"Gamma wall: ${gamma_wall:.2f}" if gamma_wall else
        f"Net gamma: {net_gamma:.2e} | No clear gamma wall"
    )

    return {
        "score": score,
        "direction": direction,
        "net_gamma": net_gamma,
        "call_gamma": call_gamma,
        "put_gamma": put_gamma,
        "gamma_wall": gamma_wall,
        "details": details,
    }


def score_market_tide(tide: list[dict]) -> dict:
    """
    Score market-wide options flow (calls vs puts premium).
    Most recent data point is most relevant.
    """
    if not tide:
        return {"score": 50, "direction": "NEUTRAL", "details": "No tide data"}

    latest = tide[-1]
    net_call = _safe_float(latest.get("net_call_premium"))
    net_put  = _safe_float(latest.get("net_put_premium"))
    total    = abs(net_call) + abs(net_put)

    if total == 0:
        return {"score": 50, "direction": "NEUTRAL", "details": "No premium data"}

    call_pct = net_call / total
    # call_pct < 0.5 = puts dominant = BEARISH
    # call_pct > 0.5 = calls dominant = BULLISH
    score = 50 + (call_pct - 0.5) * 100
    score = max(0, min(100, round(score)))

    direction = "BULLISH" if score > 57 else ("BEARISH" if score < 43 else "NEUTRAL")
    details = f"Net call: ${net_call/1e6:.1f}M | Net put: ${net_put/1e6:.1f}M"

    return {
        "score": score,
        "direction": direction,
        "net_call_premium": net_call,
        "net_put_premium": net_put,
        "details": details,
    }


def score_earnings_risk(earnings_history: list[dict]) -> dict:
    """
    Calculate earnings proximity risk (Rule 2 gate).

    < 7 days  = HIGH RISK — Rule 2 says avoid new positions
    7-14 days = MEDIUM RISK — reduce position size 50%
    > 14 days = LOW RISK — normal sizing
    """
    days = _days_to_earnings(earnings_history)

    if days is None:
        return {"days_to_earnings": None, "risk": "UNKNOWN", "block_trade": False}

    if days <= 7:
        return {"days_to_earnings": days, "risk": "HIGH", "block_trade": True,
                "reason": f"Earnings in {days} days — Rule 2: AVOID new positions"}
    elif days <= 14:
        return {"days_to_earnings": days, "risk": "MEDIUM", "block_trade": False,
                "reason": f"Earnings in {days} days — reduce size 50%"}
    else:
        return {"days_to_earnings": days, "risk": "LOW", "block_trade": False,
                "reason": f"Earnings in {days} days — normal sizing"}


# ─────────────────────────────────────────────────────────────────────────────
# Combined Scorer
# ─────────────────────────────────────────────────────────────────────────────

def score_signal_package(pkg: dict) -> dict:
    """
    Score a full signal package from get_signal_package().

    Weights:
        Options flow:  40% (most predictive — OPRA-sourced sweeps)
        Dark pool:     25% (institutional block confirmation)
        GEX:           15% (dealer positioning, structural levels)
        Market tide:   20% (macro direction of all institutional money)

    Returns:
        {
            ticker, direction, confidence (0-100),
            earnings_risk, flow, dark_pool, gex, market_tide,
            signal (STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL),
            summary (plain English),
            trade_blocked (bool — True if earnings within 7 days)
        }
    """
    ticker = pkg.get("ticker", "UNKNOWN")

    # Get current price from contracts data
    contracts = pkg.get("option_contracts", [])
    current_price = 0.0
    if contracts:
        for c in contracts:
            if c.get("avg_price"):
                current_price = _safe_float(c.get("avg_price"))
                break

    # Score each component
    flow_score  = score_flow_alerts(pkg.get("flow_alerts", []))
    dp_score    = score_dark_pool(pkg.get("dark_pool", []))
    gex_score   = score_gex(pkg.get("gex", []), pkg.get("gex_by_strike", []), current_price)
    tide_score  = score_market_tide(pkg.get("market_tide", []))
    earn_risk   = score_earnings_risk(pkg.get("earnings_history", []))

    # Weighted combined score
    combined = (
        flow_score["score"]  * 0.40 +
        dp_score["score"]    * 0.25 +
        gex_score["score"]   * 0.15 +
        tide_score["score"]  * 0.20
    )
    confidence = round(combined)

    # Direction vote
    votes = {
        "BULLISH": 0,
        "BEARISH": 0,
        "NEUTRAL": 0,
    }
    votes[flow_score["direction"]]  += 2   # flow gets 2 votes (most important)
    votes[dp_score["direction"]]    += 1
    votes[gex_score["direction"]]   += 1
    votes[tide_score["direction"]]  += 1

    direction = max(votes, key=votes.get)
    if votes["BULLISH"] == votes["BEARISH"]:
        direction = "NEUTRAL"

    # Signal label
    if earn_risk["block_trade"]:
        signal = "BLOCKED"
    elif confidence >= 75:
        signal = "STRONG_BUY" if direction == "BULLISH" else "STRONG_SELL" if direction == "BEARISH" else "NEUTRAL"
    elif confidence >= 60:
        signal = "BUY" if direction == "BULLISH" else "SELL" if direction == "BEARISH" else "NEUTRAL"
    else:
        signal = "NEUTRAL"

    # Plain-English summary
    days_to_earn = earn_risk.get("days_to_earnings")
    earn_str = f"{days_to_earn}d to earnings" if days_to_earn else "no earnings soon"

    summary = (
        f"{ticker} | {signal} | Confidence: {confidence}/100 | "
        f"Flow: {flow_score['direction']} ({flow_score['score']}) | "
        f"DP: {dp_score['direction']} ({dp_score['score']}) | "
        f"GEX: {gex_score.get('gamma_wall', 'N/A')} wall | "
        f"Earnings: {earn_str}"
    )

    return {
        "ticker": ticker,
        "direction": direction,
        "confidence": confidence,
        "signal": signal,
        "trade_blocked": earn_risk["block_trade"],
        "earnings_risk": earn_risk,
        "flow": flow_score,
        "dark_pool": dp_score,
        "gex": gex_score,
        "market_tide": tide_score,
        "summary": summary,
    }