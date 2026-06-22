"""
Active Bets — Portfolio positions with full trade context.

Answers the question: "What are my current bets and how are they doing?"

For each position shows:
    - How much invested (cost basis)
    - Current value and P&L
    - Target exit price/% (default +20% stocks, +80% options)
    - Stop loss price/% (default -40%)
    - Distance to target and stop
    - Status: TARGET_HIT / NEAR_TARGET / ON_TRACK / NEAR_STOP / STOP_HIT
    - Source: from our recommendation engine or manually opened
    - How many times we've recommended selling this (ignored signals)

Cross-references:
    - tracked_positions: custom target/stop from recommendation engine
    - sell_recommendations: past exit signals we've generated
"""
from datetime import datetime

# Default thresholds (same as sell_signals.py)
DEFAULT_TARGET_STOCK  = 20.0   # +20% for stocks
DEFAULT_TARGET_OPTION = 80.0   # +80% for options
DEFAULT_STOP          = -40.0  # -40% for all


def _get_tracked_meta(user_id: str) -> dict[str, dict]:
    """Load tracked_positions metadata keyed by symbol."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT symbol, source, target_pct, stop_pct,
                       entry_price, check_interval_min, llm_entry_note
                FROM tracked_positions
                WHERE user_id = :uid AND is_active = TRUE
            """), {"uid": user_id}).fetchall()
            return {r.symbol: dict(r._mapping) for r in rows}
    except Exception:
        return {}


def _get_past_signal_counts(user_id: str) -> dict[str, int]:
    """Count how many times we've recommended selling each symbol."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT symbol, COUNT(*) as cnt
                FROM sell_recommendations
                WHERE user_id = :uid
                GROUP BY symbol
            """), {"uid": user_id}).fetchall()
            return {r.symbol: r.cnt for r in rows}
    except Exception:
        return {}


def _classify_status(pnl_pct: float, target_pct: float, stop_pct: float) -> tuple[str, str]:
    """
    Returns (status, emoji) based on P&L vs target/stop.
    NEAR thresholds = within 20% of the level.
    """
    near_target_threshold = target_pct * 0.80
    near_stop_threshold   = stop_pct * 0.80  # e.g. -32% when stop is -40%

    if pnl_pct >= target_pct:
        return "TARGET_HIT", "🎯"
    if pnl_pct <= stop_pct:
        return "STOP_HIT", "🛑"
    if pnl_pct >= near_target_threshold:
        return "NEAR_TARGET", "🟢"
    if pnl_pct <= near_stop_threshold:
        return "NEAR_STOP", "🟡"
    return "ON_TRACK", "✅"


def get_active_bets(
    positions: list[dict],
    user_id: str | None = None,
) -> list[dict]:
    """
    Enrich all positions with target, stop, status and recommendation history.

    Args:
        positions: from WebullConnector.get_positions()
        user_id:   for DB lookups (tracked positions + past recommendations)

    Returns:
        List of enriched position dicts sorted by urgency (STOP_HIT first)
    """
    tracked_meta  = _get_tracked_meta(user_id) if user_id else {}
    past_signals  = _get_past_signal_counts(user_id) if user_id else {}

    bets = []
    for pos in positions:
        symbol    = pos.get("symbol", "")
        qty       = float(pos.get("qty", 0))
        cost      = float(pos.get("total_cost", 0))
        value     = float(pos.get("market_value", 0))
        pnl_amt   = float(pos.get("unrealized_profit_loss", 0))
        pnl_pct   = float(pos.get("unrealized_profit_loss_rate", 0)) * 100
        inst_type = pos.get("instrument_type", "STOCK")
        unit_cost = float(pos.get("unit_cost", 0))
        last_price = float(pos.get("last_price", 0))

        if qty == 0 or cost == 0:
            continue

        # Get custom targets from tracked_positions (if available)
        meta = tracked_meta.get(symbol, {})
        source     = meta.get("source", "manual")
        target_pct = float(meta.get("target_pct") or (
            DEFAULT_TARGET_OPTION if inst_type == "OPTION" else DEFAULT_TARGET_STOCK
        ))
        stop_pct   = float(meta.get("stop_pct") or DEFAULT_STOP)

        # Calculate target and stop prices
        target_price = round(unit_cost * (1 + target_pct / 100), 2) if unit_cost else None
        stop_price   = round(unit_cost * (1 + stop_pct / 100), 2) if unit_cost else None
        target_value = round(cost * (1 + target_pct / 100), 2)
        stop_value   = round(cost * (1 + stop_pct / 100), 2)

        # Distance remaining to target/stop
        dist_to_target = round(target_pct - pnl_pct, 1) if target_pct else None
        dist_to_stop   = round(pnl_pct - stop_pct, 1) if stop_pct else None

        # Potential profit/loss in $
        potential_gain = round(target_value - cost, 2)
        potential_loss = round(stop_value - cost, 2)

        # Status
        status, emoji = _classify_status(pnl_pct, target_pct, stop_pct)

        # Past recommendations (times we told user to sell this)
        past_sell_signals = past_signals.get(symbol, 0)

        bets.append({
            "symbol":          symbol,
            "instrument_type": inst_type,
            "source":          source,

            # Position size
            "qty":         qty,
            "investment":  round(cost, 2),
            "unit_cost":   unit_cost,

            # Current state
            "current_price": last_price,
            "current_value": round(value, 2),
            "pnl_amount":    round(pnl_amt, 2),
            "pnl_pct":       round(pnl_pct, 1),

            # Targets
            "target_pct":    target_pct,
            "target_price":  target_price,
            "target_value":  target_value,
            "potential_gain": potential_gain,

            # Stop loss
            "stop_pct":      stop_pct,
            "stop_price":    stop_price,
            "stop_value":    stop_value,
            "potential_loss": potential_loss,

            # Distance
            "dist_to_target_pct": dist_to_target,
            "dist_to_stop_pct":   dist_to_stop,

            # Status
            "status":  status,
            "emoji":   emoji,

            # History
            "past_sell_signals": past_sell_signals,
            "llm_entry_note":    meta.get("llm_entry_note"),
        })

    # Sort: STOP_HIT → NEAR_STOP → TARGET_HIT → NEAR_TARGET → ON_TRACK
    order = {"STOP_HIT": 0, "NEAR_STOP": 1, "TARGET_HIT": 2, "NEAR_TARGET": 3, "ON_TRACK": 4}
    bets.sort(key=lambda x: (order.get(x["status"], 5), x["pnl_pct"]))

    return bets


def format_bets_report(bets: list[dict]) -> str:
    """
    Format active bets into clean Claude Desktop output.
    """
    if not bets:
        return "No active positions found."

    lines = ["## Active Bets — Portfolio Overview"]

    # Summary counts
    hits    = [b for b in bets if b["status"] == "TARGET_HIT"]
    stops   = [b for b in bets if b["status"] == "STOP_HIT"]
    near_t  = [b for b in bets if b["status"] == "NEAR_TARGET"]
    near_s  = [b for b in bets if b["status"] == "NEAR_STOP"]
    on_t    = [b for b in bets if b["status"] == "ON_TRACK"]

    total_invested = sum(b["investment"] for b in bets)
    total_value    = sum(b["current_value"] for b in bets)
    total_pnl      = sum(b["pnl_amount"] for b in bets)

    lines.append(
        "**Total invested:** ${:,.0f} | **Current value:** ${:,.0f} | "
        "**P&L:** ${:,.0f} ({:+.1f}%)".format(
            total_invested, total_value, total_pnl,
            total_pnl / total_invested * 100 if total_invested else 0
        )
    )
    lines.append(
        "🛑 {} stop hit | 🟡 {} near stop | ✅ {} on track | "
        "🟢 {} near target | 🎯 {} target hit".format(
            len(stops), len(near_s), len(on_t), len(near_t), len(hits)
        )
    )
    lines.append("")

    # Group by status
    groups = [
        ("🛑 STOP HIT — Exit Immediately", stops),
        ("🟡 NEAR STOP — Watch Closely", near_s),
        ("🎯 TARGET HIT — Take Profit", hits),
        ("🟢 NEAR TARGET — Consider Partial Exit", near_t),
        ("✅ On Track", on_t),
    ]

    for group_title, group_bets in groups:
        if not group_bets:
            continue
        lines.append(f"### {group_title}")
        for b in group_bets:
            src = "📊 Rec" if b["source"] == "recommendation" else "👤 Manual"
            lines.append(
                "\n**{}** {} | {} | P&L: {:+.1f}% (${:+,.0f})".format(
                    b["symbol"], b["emoji"], src,
                    b["pnl_pct"], b["pnl_amount"]
                )
            )
            lines.append(
                "  💵 Invested: ${:,.0f} → Current: ${:,.0f}".format(
                    b["investment"], b["current_value"]
                )
            )
            lines.append(
                "  🎯 Target: {:+.0f}% @ ${} (${:+,.0f} gain) | "
                "🛑 Stop: {:+.0f}% @ ${} (${:,.0f} loss)".format(
                    b["target_pct"], b["target_price"] or "?",
                    b["potential_gain"],
                    b["stop_pct"], b["stop_price"] or "?",
                    abs(b["potential_loss"])
                )
            )
            lines.append(
                "  📏 Distance: {:+.1f}% to target | {:+.1f}% to stop".format(
                    b["dist_to_target_pct"] or 0,
                    b["dist_to_stop_pct"] or 0,
                )
            )
            if b["past_sell_signals"] > 0:
                lines.append(
                    "  ⚠️  We recommended selling {} time(s) — not yet acted on".format(
                        b["past_sell_signals"]
                    )
                )
            if b["llm_entry_note"]:
                lines.append(f"  💬 Entry note: {b['llm_entry_note']}")

    return "\n".join(lines)