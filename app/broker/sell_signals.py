"""
W5: Sell Signals + Portfolio P&L — Full LLM-Driven Architecture.

Philosophy:
    Rules fire first (instant) to catch clear violations.
    LLM then sees ALL 22 positions — not just flagged ones.
    LLM can surface issues rules miss: weak TA + poor sector context
    even on positions at 0% P&L.

    Future layers (plug straight into LLM prompt):
    - News sentiment per ticker
    - Historical price patterns (6-month support/resistance)
    - Macro calendar (Fed, CPI, earnings clusters)
    - Sector rotation signals

Speed:
    asyncio + OLLAMA_NUM_PARALLEL=4
    All 22 positions submitted simultaneously
    GPU processes 4 streams at once → ~15s total

TA:
    All positions (no range filter)
    Smart rate limiter: sleeps only remaining gap, never wastes time
    Polygon free tier: 5 req/min = 12s gap between calls
"""
import asyncio
from datetime import datetime, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TAKE_PROFIT_STOCK  = 0.20
TAKE_PROFIT_OPTION = 0.80
STOP_LOSS          = -0.40
LOSS_WATCH         = -0.25
EARNINGS_BUFFER    = 7
MIN_DTE            = 7


# ─────────────────────────────────────────────────────────────────────────────
# TA Signal — Polygon, smart rate limiter, ALL positions
# ─────────────────────────────────────────────────────────────────────────────

_ta_cache: dict[str, str] = {}
_ta_last_call: float = 0.0


def _get_ta_signal(ticker: str) -> str:
    """
    EMA9/21 + RSI14 from Polygon daily bars.
    Runs for ALL positions — no range filter.
    Smart rate limiter: sleeps only remaining gap in 12s window.
    Session-cached.
    """
    global _ta_cache, _ta_last_call
    if ticker in _ta_cache:
        return _ta_cache[ticker]

    try:
        import time as _t
        from app.market_data.polygon_client import get_bars

        elapsed = _t.time() - _ta_last_call
        if elapsed < 12:
            _t.sleep(12 - elapsed)  # REMOVE THIS LINE when on Polygon Starter plan ($29/mo)
        _ta_last_call = _t.time()

        from_dt = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        to_dt   = datetime.now().strftime("%Y-%m-%d")
        bars    = get_bars(ticker, 1, "day", from_dt, to_dt)

        if not bars or len(bars) < 50:
            _ta_cache[ticker] = "NEUTRAL"
            return "NEUTRAL"

        closes = [float(b.get("c", b.get("close", 0))) for b in bars]

        def ema(data, span):
            k, r = 2 / (span + 1), [data[0]]
            for p in data[1:]: r.append(p * k + r[-1] * (1 - k))
            return r

        ema9, ema21 = ema(closes, 9), ema(closes, 21)
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        avg_g  = sum(max(d, 0) for d in deltas[-14:]) / 14
        avg_l  = sum(max(-d, 0) for d in deltas[-14:]) / 14
        rsi    = 100 - (100 / (1 + avg_g / avg_l)) if avg_l > 0 else 50

        signal = "BUY" if ema9[-1] > ema21[-1] and rsi > 55 else \
                 "SELL" if ema9[-1] < ema21[-1] and rsi < 45 else "NEUTRAL"
        _ta_cache[ticker] = signal
        return signal

    except Exception:
        _ta_cache[ticker] = "NEUTRAL"
        return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# Earnings
# ─────────────────────────────────────────────────────────────────────────────

def _get_earnings_days(ticker: str) -> int | None:
    try:
        from app.options_flow.unusual_whales import get_ticker_earnings_history
        today = datetime.now().date()
        for e in (get_ticker_earnings_history(ticker) or []):
            ds = e.get("report_date") or e.get("earnings_date") or ""
            if not ds: continue
            try:
                d = datetime.strptime(ds[:10], "%Y-%m-%d").date()
                if d >= today: return (d - today).days
            except Exception: pass
        return None
    except Exception:
        return None


def _parse_option_dte(symbol: str) -> int | None:
    try:
        import re
        m = re.search(r"(\d{6})[CP]", symbol)
        if m:
            return (datetime.strptime("20" + m.group(1), "%Y%m%d") - datetime.now()).days
    except Exception: pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1: Rule-Based (instant)
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_sell_signals(positions: list[dict]) -> list[dict]:
    """Rule-based evaluation. Fast, no API calls, no LLM."""
    results = []
    for pos in positions:
        symbol    = pos.get("symbol", "")
        qty       = float(pos.get("qty", 0))
        cost      = float(pos.get("total_cost", 0))
        value     = float(pos.get("market_value", 0))
        pnl       = float(pos.get("unrealized_profit_loss", 0))
        pnl_rate  = float(pos.get("unrealized_profit_loss_rate", 0))
        inst_type = pos.get("instrument_type", "STOCK")
        ticker    = symbol.split()[0]
        if qty == 0 or cost == 0: continue

        signals, action, urgency = [], "HOLD", "LOW"

        if pnl_rate <= STOP_LOSS:
            signals.append("STOP LOSS: {:+.1f}%".format(pnl_rate * 100))
            action, urgency = "SELL", "HIGH"

        threshold = TAKE_PROFIT_OPTION if inst_type == "OPTION" else TAKE_PROFIT_STOCK
        if pnl_rate >= threshold:
            signals.append("TAKE PROFIT: {:+.1f}%".format(pnl_rate * 100))
            if action != "SELL": action, urgency = "SELL", "HIGH"

        if inst_type == "OPTION":
            dte = _parse_option_dte(symbol)
            if dte is not None and dte <= MIN_DTE:
                signals.append("DTE: {} days".format(dte))
                if action == "HOLD": action, urgency = "SELL", "HIGH"

        earn_days = _get_earnings_days(ticker)
        if earn_days is not None and earn_days <= EARNINGS_BUFFER:
            signals.append("EARNINGS IN {}d".format(earn_days))
            if action == "HOLD": action, urgency = "WATCH", "MEDIUM"

        if STOP_LOSS < pnl_rate <= LOSS_WATCH and action == "HOLD":
            signals.append("LOSS WATCH: {:+.1f}%".format(pnl_rate * 100))
            action, urgency = "WATCH", "MEDIUM"

        # TA for ALL positions (no range filter)
        if action == "HOLD" and len(ticker) <= 6:
            ta = _get_ta_signal(ticker)
            if ta == "SELL":
                signals.append("TA SELL: {} EMA9<EMA21, RSI<45".format(ticker))
                action, urgency = "WATCH", "MEDIUM"

        results.append({
            "symbol": symbol, "instrument_type": inst_type,
            "action": action, "urgency": urgency,
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_rate * 100, 2),
            "cost": round(cost, 2), "market_value": round(value, 2),
            "qty": qty, "signals": signals, "earnings_days": earn_days,
            "unit_cost": float(pos.get("unit_cost", 0)),
            "last_price": float(pos.get("last_price", 0)),
        })

    results.sort(key=lambda x: ({"HIGH":0,"MEDIUM":1,"LOW":2}.get(x["urgency"],3), x["pnl_pct"]))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Async Ollama Call
# ─────────────────────────────────────────────────────────────────────────────

async def _call_ollama_async(
    session,
    prompt: str,
    system: str,
    max_tokens: int,
    ollama_host: str,
    model: str,
) -> str:
    """Single async Ollama call. Runs concurrently with OLLAMA_NUM_PARALLEL."""
    import aiohttp
    payload = {
        "model":   model,
        "prompt":  prompt,
        "system":  system,
        "stream":  False,
        "options": {"num_predict": max_tokens, "temperature": 0.3, "top_p": 0.9},
    }
    try:
        async with session.post(
            f"{ollama_host}/api/generate",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as r:
            data = await r.json()
            return data.get("response", "")
    except Exception as e:
        return f"[error: {e}]"


def _get_past_recommendations(symbol: str, user_id: str, days_back: int = 30) -> list[dict]:
    """
    Fetch past sell recommendations for this symbol.
    Used to tell LLM if this is a repeat signal and whether user acted.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as session:
            rows = session.execute(text("""
                SELECT llm_action, llm_summary, pnl_pct,
                       user_acted, recommended_at
                FROM sell_recommendations
                WHERE user_id    = :uid
                  AND symbol     = :sym
                  AND recommended_at >= now() - :days * interval '1 day'
                ORDER BY recommended_at DESC
                LIMIT 5
            """), {"uid": user_id, "sym": symbol, "days": days_back}).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception:
        return []


def _build_signal_prompt(pos: dict, all_positions: list[dict], user_id: str | None = None) -> str:
    """Build focused per-position prompt with past recommendation history."""
    sym     = pos["symbol"]
    pnl_pct = pos["pnl_pct"]
    rules   = ", ".join(pos["signals"]) if pos["signals"] else "No rules triggered"
    earn    = "Earnings in {}d.".format(pos["earnings_days"]) if pos.get("earnings_days") else ""
    ta_sig  = next((s for s in pos["signals"] if "TA" in s), "NEUTRAL")
    peers   = " | ".join(
        "{} {:+.0f}%".format(p["symbol"], p["pnl_pct"])
        for p in all_positions[:5] if p["symbol"] != sym
    )

    # Past recommendation context
    history_text = ""
    if user_id:
        past = _get_past_recommendations(sym, user_id)
        if past:
            lines = []
            for i, p in enumerate(past, 1):
                acted  = "✅ User acted" if p.get("user_acted") else "❌ Not acted on"
                pct    = p.get("pnl_pct", 0)
                action = p.get("llm_action", "?")
                dt     = str(p.get("recommended_at", ""))[:10]
                lines.append("  #{}: {} recommended {} at {:+.1f}% — {}".format(
                    i, dt, action, pct, acted))
            times = len(past)
            history_text = (
                "\nPAST RECOMMENDATIONS ({} time{} in last 30 days):\n{}\n"
                "NOTE: {} — factor this into your recommendation urgency."
            ).format(
                times,
                "s" if times > 1 else "",
                "\n".join(lines),
                "User has NOT acted on previous signals — increase urgency" if not past[0].get("user_acted")
                else "User acted on last signal — provide fresh analysis"
            )

    return (
        "Position: {sym} | P&L: {pnl:+.1f}% | Cost: ${cost:,.0f} | Value: ${val:,.0f}\n"
        "Rule signals: {rules}\n"
        "TA: {ta} | {earn}\n"
        "Portfolio peers: {peers}"
        "{history}\n\n"
        "Reply ONLY:\n"
        "ACTION: FULL_EXIT or PARTIAL_EXIT or HOLD or ROLL\n"
        "EXIT_PCT: 0-100\n"
        "CONFIDENCE: HIGH or MEDIUM or LOW\n"
        "SUMMARY: one sentence\n"
        "REASONING: one sentence — if repeat signal, explain why user should act NOW\n"
        "RISK: one sentence"
    ).format(
        sym=sym, pnl=pnl_pct, cost=pos["cost"], val=pos["market_value"],
        rules=rules, ta=ta_sig, earn=earn, peers=peers, history=history_text
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2: Async Parallel LLM — ALL positions
# ─────────────────────────────────────────────────────────────────────────────

async def _analyze_all_async(
    signals: list[dict],
    ollama_host: str,
    model: str,
) -> list[dict]:
    """
    Submit ALL positions to Ollama simultaneously via asyncio.
    With OLLAMA_NUM_PARALLEL=4: processes 4 at once.
    All 22 positions → GPU queues → ~15s total.
    """
    import aiohttp

    # Build prompts — ALL positions, not just flagged ones
    # LLM sees everything and can surface issues rules miss
    prompts = [(_build_signal_prompt(s, signals, user_id), s) for s in signals]

    async with aiohttp.ClientSession() as session:
        tasks = [
            _call_ollama_async(
                session, prompt,
                "Expert portfolio manager. Concise, opinionated, context-aware. "
                "Override rules when context justifies it.",
                90, ollama_host, model
            )
            for prompt, _ in prompts
        ]
        responses = await asyncio.gather(*tasks)

    # Parse each response
    for (_, signal), response in zip(prompts, responses):
        parsed = {}
        for line in response.splitlines():
            line = line.strip()
            if ":" not in line: continue
            key, _, val = line.partition(":")
            key = key.strip().upper()
            val = val.strip()
            if key == "ACTION":      parsed["action"] = val
            elif key == "EXIT_PCT":
                try: parsed["exit_pct"] = int(val.replace("%",""))
                except: parsed["exit_pct"] = 100
            elif key == "CONFIDENCE": parsed["confidence"] = val
            elif key == "SUMMARY":    parsed["summary"] = val
            elif key == "REASONING":  parsed["reasoning"] = val
            elif key == "RISK":       parsed["risk"] = val

        signal["llm"] = {
            "action":     parsed.get("action", signal["action"]),
            "exit_pct":   parsed.get("exit_pct", 100),
            "confidence": parsed.get("confidence", "MEDIUM"),
            "summary":    parsed.get("summary", ""),
            "reasoning":  parsed.get("reasoning", ""),
            "risk":       parsed.get("risk", ""),
        }

        # If LLM upgrades HOLD → WATCH/SELL, update action
        llm_action = parsed.get("action", "")
        if signal["action"] == "HOLD" and llm_action in ("FULL_EXIT", "PARTIAL_EXIT"):
            signal["action"]  = "SELL"
            signal["urgency"] = "MEDIUM"
            if not signal["signals"]:
                signal["signals"].append("LLM-INITIATED: no rule triggered but LLM recommends exit")

    return signals


def evaluate_sell_signals_with_llm(
    positions: list[dict],
    user_id: str | None = None,
) -> list[dict]:
    """
    Full LLM-driven sell analysis.

    1. Rule-based signals (instant) — catches clear violations
    2. TA for ALL positions via Polygon (smart rate limited)
    3. Async parallel LLM on ALL positions — sees full context,
       can surface issues rules miss

    Requires: OLLAMA_NUM_PARALLEL=4 for full parallelism benefit.
    """
    from app.utils.config import settings

    signals = evaluate_sell_signals(positions)

    # Flag repeat signals — adds urgency before LLM sees them
    if user_id:
        for s in signals:
            if s["action"] in ("SELL", "WATCH"):
                past      = _get_past_recommendations(s["symbol"], user_id)
                not_acted = [p for p in past if not p.get("user_acted")]
                if not_acted:
                    s["signals"].insert(0,
                        "REPEAT #{} — recommended {} time(s) in last 30 days, not yet acted on".format(
                            len(not_acted) + 1, len(not_acted)))
                    s["repeat_count"] = len(not_acted)

    try:
        ollama_host = getattr(settings, "ollama_host", "http://localhost:11434")
        model       = getattr(settings, "ollama_model", "qwen2.5:14b")
        signals     = asyncio.run(_analyze_all_async(signals, ollama_host, model))
    except Exception as e:
        for s in signals:
            s["llm"] = {"error": str(e)}

    # Log to DB
    if user_id:
        try: log_sell_recommendations(signals, user_id)
        except Exception: pass

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio P&L
# ─────────────────────────────────────────────────────────────────────────────

def get_portfolio_pnl_summary(
    positions: list[dict],
    balances: dict | None = None,
) -> dict:
    if not positions:
        return {"error": "No positions found"}

    total_cost, total_value, total_pnl = 0.0, 0.0, 0.0
    breakdown = []

    for pos in positions:
        cost     = float(pos.get("total_cost", 0))
        value    = float(pos.get("market_value", 0))
        pnl      = float(pos.get("unrealized_profit_loss", 0))
        pnl_rate = float(pos.get("unrealized_profit_loss_rate", 0))
        total_cost  += cost
        total_value += value
        total_pnl   += pnl
        breakdown.append({
            "symbol":     pos.get("symbol"),
            "type":       pos.get("instrument_type", "STOCK"),
            "qty":        float(pos.get("qty", 0)),
            "cost":       round(cost, 2),
            "value":      round(value, 2),
            "pnl":        round(pnl, 2),
            "pnl_pct":    round(pnl_rate * 100, 2),
            "weight":     0.0,
            "unit_cost":  float(pos.get("unit_cost", 0)),
            "last_price": float(pos.get("last_price", 0)),
        })

    for b in breakdown:
        b["weight"] = round(b["value"] / max(total_value, 1) * 100, 1)
    breakdown.sort(key=lambda x: x["pnl"], reverse=True)

    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0
    winners = [b for b in breakdown if b["pnl"] > 0]
    losers  = [b for b in breakdown if b["pnl"] < 0]

    return {
        "total_cost":      round(total_cost, 2),
        "total_value":     round(total_value, 2),
        "total_pnl":       round(total_pnl, 2),
        "total_pnl_pct":   round(total_pnl_pct, 2),
        "position_count":  len(positions),
        "buying_power":    balances.get("buying_power") if balances else None,
        "cash":            balances.get("cash_balance") if balances else None,
        "account_value":   balances.get("total_account_value") if balances else None,
        "winners":         len(winners),
        "losers":          len(losers),
        "win_rate":        round(len(winners) / max(len(positions), 1) * 100, 1),
        "best_performer":  breakdown[0]["symbol"] if breakdown else None,
        "worst_performer": breakdown[-1]["symbol"] if breakdown else None,
        "biggest_gain":    round(max((b["pnl"] for b in breakdown), default=0), 2),
        "biggest_loss":    round(min((b["pnl"] for b in breakdown), default=0), 2),
        "positions":       breakdown,
        "as_of":           datetime.now().strftime("%Y-%m-%d %H:%M ET"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Recommendation Storage
# ─────────────────────────────────────────────────────────────────────────────

def log_sell_recommendations(signals: list[dict], user_id: str) -> None:
    """Log all SELL/WATCH signals to DB. Outcomes filled in by C10 later."""
    import json
    from sqlalchemy import text
    from app.db.session import get_session

    to_log = [s for s in signals if s["action"] in ("SELL", "WATCH")]
    if not to_log: return

    with get_session() as session:
        for s in to_log:
            llm = s.get("llm", {})
            session.execute(text("""
                INSERT INTO sell_recommendations (
                    user_id, symbol, instrument_type,
                    cost_basis, market_value, pnl_pct,
                    rule_signals, llm_action, llm_exit_pct,
                    llm_summary, llm_confidence
                ) VALUES (
                    :uid, :sym, :itype, :cost, :val, :pnl,
                    :rules, :action, :exit_pct, :summary, :conf
                )
            """), {
                "uid": user_id, "sym": s["symbol"], "itype": s["instrument_type"],
                "cost": s["cost"], "val": s["market_value"], "pnl": s["pnl_pct"],
                "rules": json.dumps(s["signals"]),
                "action": llm.get("action"), "exit_pct": llm.get("exit_pct"),
                "summary": llm.get("summary"), "conf": llm.get("confidence"),
            })

    print("[Sell] {} signals logged".format(len(to_log)))


def get_recommendation_history(user_id: str, symbol: str | None = None, days_back: int = 30) -> list[dict]:
    from sqlalchemy import text
    from app.db.session import get_session
    with get_session() as session:
        rows = session.execute(text("""
            SELECT symbol, pnl_pct, llm_action, llm_exit_pct,
                   llm_summary, llm_confidence, user_acted,
                   was_correct, recommended_at
            FROM sell_recommendations
            WHERE user_id = :uid
              AND recommended_at >= now() - :days * interval '1 day'
              AND (:sym IS NULL OR symbol = :sym)
            ORDER BY recommended_at DESC LIMIT 100
        """), {"uid": user_id, "days": days_back, "sym": symbol}).fetchall()
        return [dict(r._mapping) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Report Formatter
# ─────────────────────────────────────────────────────────────────────────────

def format_sell_report(signals: list[dict], pnl: dict) -> str:
    lines = ["## Portfolio P&L Summary"]
    lines.append(
        "**Total Value:** ${:,.2f} | **P&L:** ${:,.2f} ({:+.2f}%) | "
        "**Win Rate:** {}% ({}W/{}L)".format(
            pnl["total_value"], pnl["total_pnl"], pnl["total_pnl_pct"],
            pnl["win_rate"], pnl["winners"], pnl["losers"]
        )
    )
    if pnl.get("buying_power"):
        lines.append("**Buying Power:** ${:,.2f}".format(float(pnl["buying_power"])))
    lines.append("")

    actionable = [s for s in signals if s["action"] in ("SELL","WATCH")]
    holds      = [s for s in signals if s["action"] == "HOLD"]

    if actionable:
        lines.append("## Exit Recommendations")
        for s in actionable:
            emoji  = "🔴" if s["action"] == "SELL" else "🟡"
            repeat = " 🔁 x{}".format(s["repeat_count"]) if s.get("repeat_count") else ""
            lines.append("\n{} **{}{}** — {} ({}) | P&L: {:+.1f}%".format(
                emoji, s["symbol"], repeat, s["action"], s["urgency"], s["pnl_pct"]))
            for sig in s["signals"]:
                lines.append("  - {}".format(sig))
            llm = s.get("llm")
            if llm and not llm.get("error"):
                lines.append("  💬 **LLM:** {}".format(llm.get("summary","")))
                if llm.get("exit_pct", 100) < 100:
                    lines.append("  📊 Exit {}% | Confidence: {}".format(
                        llm["exit_pct"], llm.get("confidence","")))
                if llm.get("reasoning"):
                    lines.append("  📝 {}".format(llm["reasoning"]))
                if llm.get("risk"):
                    lines.append("  ⚠️  Risk: {}".format(llm["risk"]))
    else:
        lines.append("## Exit Recommendations\n✅ No urgent exit signals.")

    if holds:
        lines.append("\n## Holds — LLM Assessment")
        for s in holds:
            llm = s.get("llm", {})
            llm_action = llm.get("action", "HOLD")
            indicator  = "✅" if llm_action == "HOLD" else "⚡"
            lines.append("{} **{}** {:+.1f}% — {}".format(
                indicator, s["symbol"], s["pnl_pct"], llm.get("summary", "Hold.")))

    return "\n".join(lines)