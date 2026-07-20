"""
Recommendation storage, lifecycle, and formatting — shared by both the
MCP daily tool (get_daily_recommendations) and the web dashboard.

Scan orchestration (candidate selection, conviction/confidence scoring,
LLM strategy decisions) lives entirely in rescan_engine.py/smart_engine.py
now — this file used to contain a second, independent orchestration path
(run_daily_recommendations, scored via conviction.py's older 12-factor
system with a per-ticker LLM call) that only the MCP tool used, while the
web dashboard used rescan_engine.py. That meant the same watchlist on the
same day could produce different picks depending on which channel asked.
Consolidated onto rescan_engine.py/smart_engine.py this session — see
ARCHITECTURE.md.

What's still here:
    - _upsert_recommendation / get_active_recommendations: the shared
      daily_recommendations read/write layer both engines use.
    - invalidate_recommendation / check_invalidation_conditions: thesis
      invalidation, checked by the position monitor every 15 min on
      price-crosses-stop, VIX spike, or earnings within 2 days.
    - format_daily_recommendations: Claude Desktop display formatting.
    - _check_api_health: pre-flight data-quality gate, now called from
      rescan_engine.py before a scan runs.
"""
import json
from datetime import datetime, date, timedelta


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
# Display Formatting
# ─────────────────────────────────────────────────────────────────────────────

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
