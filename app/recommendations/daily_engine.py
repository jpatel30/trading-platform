"""
Phase A — Daily Recommendation Engine.

One thesis per ticker per user per day.
Persists until invalidated by material change.
Filters out low-conviction picks before showing to consumer.

Flow:
    1. Get scanner top picks
    2. Build RAG context per pick
    3. Calculate conviction score (0-100)
    4. Filter: only score >= 70 surfaces to consumer
    5. Generate thesis (LLM: entry zone, target, stop, invalidation)
    6. Store in daily_recommendations (one row per ticker per day)
    7. Return top 5 by conviction score

Invalidation (checked by position monitor every 15 min):
    - Price crosses stop level
    - Price crosses invalidation price
    - VIX spikes past EXTREME
    - Earnings within 2 days
    → Discord alert: "Thesis invalidated — book profit or loss?"
"""
import json
from datetime import datetime, date, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# Thesis Generation
# ─────────────────────────────────────────────────────────────────────────────

def _generate_thesis(
    ticker: str,
    direction: str,
    rec: dict,
    price_ctx: dict,
    vix_ctx: dict,
    iv_ctx: dict,
    conviction: dict,
) -> dict:
    """
    Generate a complete thesis from the recommendation data.
    LLM produces the thesis text; Python extracts the levels.
    """
    best    = rec.get("best", {})
    spot    = rec.get("spot", 0)
    nearest_support    = price_ctx.get("nearest_support")
    nearest_resistance = price_ctx.get("nearest_resistance")
    entry_trigger      = price_ctx.get("entry_trigger", "BETWEEN_LEVELS")
    entry_note         = price_ctx.get("entry_note", "")
    vix_level          = vix_ctx.get("current", 17)
    vix_zone           = vix_ctx.get("zone", "NORMAL")
    iv_rank            = iv_ctx.get("iv_rank") or 50

    # Entry zone: current price ± 0.5%
    entry_zone_low  = round(spot * 0.995, 2)
    entry_zone_high = round(spot * 1.005, 2)

    # If AT_RESISTANCE (bearish) use resistance as upper entry bound
    if direction == "BEARISH" and entry_trigger in ("AT_RESISTANCE", "NEAR_RESISTANCE"):
        entry_zone_low  = round(spot * 0.995, 2)
        entry_zone_high = round((nearest_resistance or spot * 1.01), 2)

    # If AT_SUPPORT (bullish) use support as lower entry bound
    if direction == "BULLISH" and entry_trigger in ("AT_SUPPORT", "NEAR_SUPPORT"):
        entry_zone_low  = round((nearest_support or spot * 0.99), 2)
        entry_zone_high = round(spot * 1.005, 2)

    # Target and stop from strategy engine
    strategy  = best.get("strategy", "")
    legs      = best.get("legs", [])
    buy_leg   = next((l for l in legs if l["action"] == "BUY"), {})
    sell_leg  = next((l for l in legs if l["action"] == "SELL"), {})

    if direction == "BEARISH":
        target_price = nearest_support or round(spot * 0.95, 2)
        stop_price   = nearest_resistance or round(spot * 1.03, 2)
    else:
        target_price = nearest_resistance or round(spot * 1.05, 2)
        stop_price   = nearest_support or round(spot * 0.97, 2)

    target_pct = round((target_price - spot) / spot * 100, 1) if spot else 0
    stop_pct   = round((stop_price - spot) / spot * 100, 1) if spot else 0

    # Invalidation conditions
    inv_conditions = []
    if direction == "BEARISH":
        inv_conditions.append(
            f"{ticker} closes ABOVE ${stop_price} (resistance broken — thesis invalid)"
        )
    else:
        inv_conditions.append(
            f"{ticker} closes BELOW ${stop_price} (support broken — thesis invalid)"
        )
    if vix_zone in ("LOW", "NORMAL"):
        inv_conditions.append("VIX spikes above 30 (regime change to extreme fear)")
    inv_conditions.append("Earnings announced within 2 days (IV crush risk)")

    # Build thesis text
    ta_summary   = rec.get("ta_summary", "")
    flow_summary = rec.get("flow_summary", "")
    key_news     = best.get("llm_decision", {}).get("key_news", "NONE")
    reasoning    = best.get("llm_decision", {}).get("reasoning", "")

    breakdown    = conviction.get("breakdown", {})
    top_signals  = sorted(
        [(k, v) for k, v in breakdown.items() if v["score"] >= 0.7],
        key=lambda x: x[1]["points"], reverse=True
    )[:3]
    signal_text  = ", ".join(k.replace("_", " ") for k, _ in top_signals)

    thesis = (
        f"{ticker} {direction.lower()} thesis: {reasoning} "
        f"Key signals: {signal_text}. "
        f"Entry at ${entry_zone_low}-{entry_zone_high} ({entry_note}). "
        f"VIX {vix_level} ({vix_zone}), IV rank {iv_rank:.0f}/100."
    )
    if key_news and key_news != "NONE":
        thesis += f" Key catalyst: {key_news}"

    return {
        "thesis":                thesis,
        "entry_zone_low":        entry_zone_low,
        "entry_zone_high":       entry_zone_high,
        "entry_trigger":         entry_trigger,
        "target_price":          target_price,
        "target_pct":            target_pct,
        "stop_price":            stop_price,
        "stop_pct":              stop_pct,
        "timeframe":             f"{best.get('dte', 21)} days ({best.get('expiry', '')})",
        "invalidation_conditions": " | ".join(inv_conditions),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Store / Load Recommendations
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_recommendation(user_id: str, data: dict) -> str | None:
    """Store recommendation — update if exists today, insert if new."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            result = s.execute(text("""
                INSERT INTO daily_recommendations (
                    user_id, ticker, date, horizon,
                    direction, conviction_score, conviction_tier,
                    act_now, position_size_guidance,
                    thesis, entry_zone_low, entry_zone_high, entry_trigger,
                    target_price, target_pct, stop_price, stop_pct,
                    timeframe, invalidation_conditions,
                    strategy, expiry, dte, legs,
                    entry_debit, webull_limit_price, total_cost, max_profit, max_loss,
                    risk_reward, webull_instructions, key_news,
                    conviction_breakdown, signal_data, warnings, status
                ) VALUES (
                    :uid, :ticker, CURRENT_DATE, :horizon,
                    :direction, :conviction_score, :conviction_tier,
                    :act_now, :position_size,
                    :thesis, :entry_low, :entry_high, :entry_trigger,
                    :target_price, :target_pct, :stop_price, :stop_pct,
                    :timeframe, :invalidation,
                    :strategy, :expiry, :dte, :legs,
                    :entry_debit, :webull_limit_price, :total_cost, :max_profit, :max_loss,
                    :risk_reward, :webull_instructions, :key_news,
                    :breakdown, :signal_data, :warnings, 'ACTIVE'
                )
                ON CONFLICT (user_id, ticker, date, horizon)
                DO UPDATE SET
                    conviction_score        = EXCLUDED.conviction_score,
                    conviction_tier         = EXCLUDED.conviction_tier,
                    act_now                 = EXCLUDED.act_now,
                    thesis                  = EXCLUDED.thesis,
                    conviction_breakdown    = EXCLUDED.conviction_breakdown,
                    webull_limit_price      = EXCLUDED.webull_limit_price,
                    last_checked_at         = now()
                RETURNING id
            """), {
                "uid":                user_id,
                "ticker":             data["ticker"],
                "horizon":            data.get("horizon", "1m"),
                "direction":          data["direction"],
                "conviction_score":   data["conviction_score"],
                "conviction_tier":    data["conviction_tier"],
                "act_now":            data["act_now"],
                "position_size":      data.get("position_size_guidance"),
                "thesis":             data["thesis"],
                "entry_low":          data.get("entry_zone_low"),
                "entry_high":         data.get("entry_zone_high"),
                "entry_trigger":      data.get("entry_trigger"),
                "target_price":       data.get("target_price"),
                "target_pct":         data.get("target_pct"),
                "stop_price":         data.get("stop_price"),
                "stop_pct":           data.get("stop_pct"),
                "timeframe":          data.get("timeframe"),
                "invalidation":       data.get("invalidation_conditions"),
                "strategy":           data.get("strategy"),
                "expiry":             data.get("expiry"),
                "dte":                data.get("dte"),
                "legs":               json.dumps(data.get("legs", [])),
                "entry_debit":        data.get("entry_debit"),
                "webull_limit_price": data.get("webull_limit_price"),
                "total_cost":         data.get("total_cost"),
                "max_profit":         data.get("max_profit"),
                "max_loss":           data.get("max_loss"),
                "risk_reward":        data.get("risk_reward"),
                "webull_instructions": data.get("webull_instructions"),
                "key_news":           data.get("key_news"),
                "breakdown":          json.dumps(data.get("conviction_breakdown", {})),
                "signal_data":        json.dumps(data.get("signal_data", {})),
                "warnings":           json.dumps(data.get("warnings", [])),
            })
            row = result.fetchone()
        return str(row.id) if row else None
    except Exception as e:
        print(f"[DailyRec] Store failed for {data.get('ticker')}: {e}")
        return None


def get_active_recommendations(user_id: str, date_str: str | None = None) -> list[dict]:
    """Load today's active recommendations for a user."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        target_date = date_str or date.today().isoformat()
        with get_session() as s:
            rows = s.execute(text("""
                SELECT id, ticker, direction, conviction_score, conviction_tier,
                       act_now, position_size_guidance,
                       thesis, entry_zone_low, entry_zone_high, entry_trigger,
                       target_price, target_pct, stop_price, stop_pct,
                       timeframe, invalidation_conditions,
                       strategy, expiry, dte, entry_debit, webull_limit_price, total_cost,
                       max_profit, max_loss, risk_reward, webull_instructions,
                       key_news, status, invalidated_reason, warnings, created_at
                FROM daily_recommendations
                WHERE user_id   = :uid
                  AND date       = :dt
                  AND status    != 'INVALIDATED'
                ORDER BY conviction_score DESC
            """), {"uid": user_id, "dt": target_date}).fetchall()

            return [dict(r._mapping) for r in rows]
    except Exception as e:
        print(f"[DailyRec] Load failed: {e}")
        return []


def invalidate_recommendation(
    user_id: str,
    ticker: str,
    reason: str,
    notify: bool = True,
) -> bool:
    """
    Mark a recommendation as invalidated (thesis broken).
    Fires Discord alert asking user what they want to do.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        with get_session() as s:
            result = s.execute(text("""
                UPDATE daily_recommendations
                SET status             = 'INVALIDATED',
                    invalidated_reason = :reason,
                    invalidated_at     = now()
                WHERE user_id = :uid
                  AND ticker  = :ticker
                  AND date    = CURRENT_DATE
                  AND status  = 'ACTIVE'
            """), {"uid": user_id, "ticker": ticker, "reason": reason})
            updated = result.rowcount

        if updated > 0 and notify:
            _send_invalidation_alert(user_id, ticker, reason)

        return updated > 0
    except Exception as e:
        print(f"[DailyRec] Invalidation failed: {e}")
        return False


def _send_invalidation_alert(user_id: str, ticker: str, reason: str) -> None:
    """Discord alert when thesis is invalidated — ask user to book profit or loss."""
    try:
        from app.notifications.discord import get_webhook, send_discord
        webhook = get_webhook(user_id)
        if not webhook:
            return
        msg = (
            f"Thesis invalidated for {ticker}: {reason}. "
            f"Please review your position and tell me: "
            f"'I sold {ticker} at $X' to log the outcome, "
            f"or 'hold {ticker}' if you believe the thesis is still valid."
        )
        send_discord(
            webhook_url = webhook,
            symbol      = ticker,
            alert_type  = "THESIS_INVALIDATED",
            urgency     = "HIGH",
            message     = msg,
        )
    except Exception as e:
        print(f"[DailyRec] Invalidation alert failed: {e}")


def check_invalidation_conditions(user_id: str, positions: list[dict]) -> int:
    """
    Called by position monitor every poll.
    Checks all active daily_recommendations against current prices.
    Returns count of invalidated recommendations.
    """
    invalidated = 0
    try:
        recs = get_active_recommendations(user_id)
        prices = {p["symbol"]: float(p.get("last_price", 0)) for p in positions}

        for rec in recs:
            ticker      = rec["ticker"]
            direction   = rec["direction"]
            stop_price  = float(rec.get("stop_price") or 0)
            current     = prices.get(ticker)

            if not current or not stop_price:
                continue

            # Check stop level breach
            if direction == "BEARISH" and current > stop_price:
                reason = (
                    f"{ticker} at ${current:.2f} — closed above stop "
                    f"${stop_price:.2f} (resistance broken)"
                )
                if invalidate_recommendation(user_id, ticker, reason):
                    invalidated += 1
                    print(f"[DailyRec] Invalidated {ticker}: {reason}")

            elif direction == "BULLISH" and current < stop_price:
                reason = (
                    f"{ticker} at ${current:.2f} — fell below stop "
                    f"${stop_price:.2f} (support broken)"
                )
                if invalidate_recommendation(user_id, ticker, reason):
                    invalidated += 1
                    print(f"[DailyRec] Invalidated {ticker}: {reason}")

    except Exception as e:
        print(f"[DailyRec] Invalidation check failed: {e}")

    return invalidated


# ─────────────────────────────────────────────────────────────────────────────
# Main Daily Engine
# ─────────────────────────────────────────────────────────────────────────────

def run_daily_recommendations(
    user_id: str,
    budget:  float = 2000,
    top_n:   int   = 5,
    force_refresh: bool = False,
) -> dict:
    """
    Main entry point — generates today's top recommendations.

    1. Check if we already have fresh recs for today (skip if force_refresh=False)
    2. Run scanner to get top picks
    3. Build RAG context + conviction score per pick
    4. Filter: only conviction >= 70 surfaces
    5. Generate thesis for each passing pick
    6. Store in daily_recommendations
    7. Return top 5 by conviction

    Returns:
        recommendations: list of top-N picks with full thesis
        filtered_out:    picks below conviction threshold (with reason)
        data_quality:    API health indicators
    """
    from app.recommendations.conviction import (
        calculate_conviction, get_learned_weights,
        MIN_CONVICTION_TO_SURFACE, TOP_N_PER_DAY
    )
    from app.rag.context_builder import (
        build_ticker_context, _build_vix_context, clear_cache
    )
    from app.scanner.quick_scan import quick_scan
    from app.scanner.universe import get_scan_universe
    from app.options_flow.unusual_whales import get_signal_package
    from app.options_flow.signals import score_signal_package
    from app.strategy.engine import build_recommendation
    from app.market_data.uw_market_data import get_bars
    from app.technical_analysis.engine import get_technical_profile
    from datetime import timedelta
    import time

    t0 = time.time()

    # Check if we already have today's recs (unless force refresh)
    if not force_refresh:
        existing = get_active_recommendations(user_id)
        if existing:
            return {
                "recommendations": existing,
                "source":          "cached",
                "message":         f"{len(existing)} active recommendations from today. Use force_refresh=True to re-run.",
                "elapsed":         0,
            }

    # Data quality check
    api_health = _check_api_health()
    if api_health["data_quality"] < 0.5:
        return {
            "recommendations": [],
            "error":           "insufficient_data",
            "message":         f"API health too low ({api_health['data_quality']:.0%}) — retry at {api_health['retry_at']}",
            "api_health":      api_health,
        }

    print(f"[DailyRec] Running daily recommendations for user {user_id[:8]}...")

    # Step 1: Get universe — exclude portfolio positions from BUY candidates
    # (don't recommend buying what user already owns as a new entry)
    tickers = get_scan_universe(user_id=user_id)

    # Remove tickers already in portfolio
    try:
        from app.broker.webull_connector import WebullConnector
        positions     = WebullConnector(user_id).get_positions()
        portfolio_syms = {p["symbol"] for p in positions}
        tickers_filtered = [t for t in tickers if t not in portfolio_syms]
        removed = len(tickers) - len(tickers_filtered)
        if removed > 0:
            print(f"[DailyRec] Removed {removed} portfolio tickers from buy universe")
        tickers = tickers_filtered
    except Exception as e:
        print(f"[DailyRec] Portfolio filter failed: {e} — using full universe")

    # Remove market proxies unless specific catalyst
    MARKET_PROXIES = {"SPY","QQQ","IWM","DIA","VXX","UVXY","SQQQ","TQQQ","SPXU"}
    tickers = [t for t in tickers if t not in MARKET_PROXIES]

    picks = quick_scan(tickers, user_id=user_id, top_n=15)

    # Bug 3: No-signal day — never force a recommendation
    if not picks or len(picks) < 3:
        return {
            "recommendations": [],
            "message":         (
                "Scanner returned fewer than 3 signals today — "
                "insufficient conviction to recommend. "
                "Wait for better market conditions (typically Monday AM)."
            ),
            "elapsed":         round(time.time()-t0, 1),
        }

    # Step 2: VIX once for all picks
    vix_ctx = _build_vix_context()

    # Step 3: Score each pick
    weights      = get_learned_weights(user_id)
    scored       = []
    filtered_out = []

    for pick in picks:
        ticker    = pick["ticker"]
        direction = pick.get("direction", "NEUTRAL")

        try:
            # RAG context
            clear_cache()
            ctx       = build_ticker_context(ticker, include_global_news=True)
            price_ctx = ctx.get("price", {})
            iv_ctx    = ctx.get("iv", {})

            # TA data
            from_date = (datetime.now()-timedelta(days=300)).strftime('%Y-%m-%d')
            to_date   = datetime.now().strftime('%Y-%m-%d')
            bars      = get_bars(ticker, 1, 'day', from_date, to_date)
            ta_data   = get_technical_profile(ticker, bars) if bars else {}

            # Signal data from scanner pick
            signal_data = {
                "flow_score": pick.get("flow_score", 0),
                "dp_score":   pick.get("dp_score", 0),
            }

            # LLM confidence from strategy engine (quick run for confidence only)
            try:
                signal = score_signal_package(get_signal_package(ticker))
                rec    = build_recommendation(
                    ticker, ta_data, signal,
                    budget=budget, user_id=user_id
                )
                llm_conf = rec.get("best", {}).get("llm_decision", {}).get("confidence", 50) or 50
            except Exception:
                rec, llm_conf = {}, 50

            # Conviction score
            conviction = calculate_conviction(
                price_ctx   = price_ctx,
                vix_ctx     = vix_ctx,
                iv_ctx      = iv_ctx,
                ta_data     = ta_data,
                signal_data = signal_data,
                direction   = direction,
                llm_confidence = llm_conf,
                weights     = weights,
            )

            score = conviction["conviction_score"]
            print(f"  {ticker:6} {direction:8} conviction={score}/100 "
                  f"tier={conviction['conviction_tier']}")

            if score < MIN_CONVICTION_TO_SURFACE:
                filtered_out.append({
                    "ticker":     ticker,
                    "direction":  direction,
                    "conviction": score,
                    "tier":       conviction["conviction_tier"],
                    "reason":     f"Conviction {score}/100 below threshold {MIN_CONVICTION_TO_SURFACE}",
                })
                continue

            # Generate thesis for picks that pass
            thesis_data = _generate_thesis(
                ticker, direction, rec, price_ctx, vix_ctx, iv_ctx, conviction
            )
            best = rec.get("best", {})

            # Build full record
            record = {
                "ticker":             ticker,
                "direction":          direction,
                "conviction_score":   score,
                "conviction_tier":    conviction["conviction_tier"],
                "act_now":            conviction["act_now"],
                "position_size_guidance": conviction["position_size"],
                "horizon":            "1m",
                **thesis_data,
                "strategy":           best.get("strategy"),
                "expiry":             str(best.get("expiry", "")),
                "dte":                best.get("dte"),
                "legs":               best.get("legs", []),
                "entry_debit":        best.get("entry_debit"),
                "total_cost":         best.get("total_cost"),
                "max_profit":         best.get("target_profit"),
                "max_loss":           best.get("stop_loss"),
                "risk_reward":        best.get("risk_reward"),
                "webull_instructions": best.get("webull_instructions"),
                "key_news":           best.get("llm_decision", {}).get("key_news", "NONE"),
                "conviction_breakdown": conviction["breakdown"],
                "signal_data":        signal_data,
                "warnings":           rec.get("warnings", []),
                # Confirmation enforcement
                "pending_confirmation": False,
                "next_action":         "review_and_decide",
            }

            # Store in DB
            rec_id = _upsert_recommendation(user_id, record)
            record["id"] = rec_id
            scored.append(record)

        except Exception as e:
            print(f"[DailyRec] Failed for {ticker}: {e}")
            continue

    # Sort by conviction, take top N
    scored.sort(key=lambda x: x["conviction_score"], reverse=True)
    top_picks = scored[:top_n]

    # Mark top picks as needing confirmation prompt
    for pick in top_picks:
        if pick.get("act_now"):
            pick["next_action"] = "ask_user_if_filled"
            pick["confirmation_prompt"] = (
                f"Did you execute the {pick.get('strategy')} on {pick['ticker']}? "
                f"Reply 'Yes I bought X contracts at $Y' to start tracking, or 'No' to pass."
            )

    elapsed = round(time.time()-t0, 1)
    print(f"[DailyRec] Done in {elapsed}s — {len(top_picks)} recs surfaced, "
          f"{len(filtered_out)} filtered out")

    return {
        "recommendations": top_picks,
        "filtered_out":    filtered_out,
        "total_scored":    len(picks),
        "passed":          len(scored),
        "surfaced":        len(top_picks),
        "elapsed":         elapsed,
        "api_health":      api_health,
        "source":          "fresh",
        "date":            date.today().isoformat(),
    }


def format_daily_recommendations(result: dict) -> str:
    """Format daily recommendations for Claude Desktop display."""
    recs = result.get("recommendations", [])

    if not recs:
        msg = result.get("message", "No recommendations today.")
        if result.get("error") == "insufficient_data":
            return f"⚠️ {msg}"
        return f"No actionable recommendations today. {msg}"

    lines = [
        f"## Daily Recommendations — {result.get('date', date.today().isoformat())}",
        f"*{len(recs)} high-conviction picks | {len(result.get('filtered_out',[]))} filtered out (below 70/100)*",
        "",
    ]

    for i, rec in enumerate(recs, 1):
        tier_emoji = {
            "VERY_HIGH": "🔥", "HIGH": "✅", "MODERATE": "⚠️",
            "WATCH": "👁️", "SKIP": "❌"
        }.get(rec.get("conviction_tier"), "📊")

        lines.append(
            f"### {i}. {rec['ticker']} — {rec.get('direction')} "
            f"{tier_emoji} {rec.get('conviction_score')}/100 ({rec.get('conviction_tier')})"
        )
        lines.append(f"**Thesis:** {rec.get('thesis', 'N/A')}")
        lines.append("")
        lines.append(
            f"**Entry zone:** ${rec.get('entry_zone_low')} – ${rec.get('entry_zone_high')} "
            f"| {rec.get('entry_trigger', '')}"
        )
        lines.append(
            f"**Target:** ${rec.get('target_price')} ({rec.get('target_pct'):+.1f}%) "
            f"| **Stop:** ${rec.get('stop_price')} ({rec.get('stop_pct'):+.1f}%)"
        )
        lines.append(f"**Strategy:** {rec.get('strategy')} — {rec.get('timeframe')}")
        if rec.get("webull_instructions"):
            lines.append(f"**Webull:** {rec.get('webull_instructions')}")
        lines.append(f"**Invalidated if:** {rec.get('invalidation_conditions', 'N/A')}")
        if rec.get("key_news") and rec["key_news"] != "NONE":
            lines.append(f"**Key news:** {rec['key_news']}")
        if rec.get("act_now") and rec.get("confirmation_prompt"):
            lines.append(f"\n💬 *{rec['confirmation_prompt']}*")
        lines.append("")

    return "\n".join(lines)


def _check_api_health() -> dict:
    """Quick health check before running recommendations."""
    import requests, time
    from app.utils.config import settings

    checks = {}
    now    = datetime.now()

    # Polygon
    try:
        r = requests.get(
            "https://api.polygon.io/v2/aggs/ticker/SPY/prev",
            params={"apiKey": settings.polygon_api_key}, timeout=5
        )
        checks["polygon"] = r.status_code == 200
    except Exception:
        checks["polygon"] = False

    # UW
    try:
        from app.options_flow.unusual_whales import get_market_tide
        tide = get_market_tide()
        checks["uw"] = bool(tide)
    except Exception:
        checks["uw"] = False

    # yfinance VIX
    try:
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(period="1d")
        checks["yfinance"] = not hist.empty
    except Exception:
        checks["yfinance"] = False

    passing       = sum(1 for v in checks.values() if v)
    data_quality  = passing / len(checks)
    # Retry in 15 min if rate limited
    retry_at      = (now + timedelta(minutes=15)).strftime("%H:%M")

    return {
        "checks":       checks,
        "data_quality": data_quality,
        "retry_at":     retry_at,
        "ok":           data_quality >= 0.5,
    }
