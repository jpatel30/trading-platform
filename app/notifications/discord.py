"""
C14 Notification Service — Discord (W14).

Sends rich Discord embeds when position_monitor fires alerts.
Color-coded by urgency, grouped by alert type.

Webhook URL stored in user_api_keys table (key_type = 'discord_webhook').

Alert routing:
    HIGH   (STOP_LOSS, TAKE_PROFIT, DTE_WARNING) → Discord + @mention
    MEDIUM (NEAR_STOP, EARNINGS, TA_REVERSAL)    → Discord
    LOW    → silent (no notification)

Phase 2 (dashboard):
    WebSocket  → in-app real-time bell
    FCM Push   → browser push when app closed
"""
import requests
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────────────
# Discord Embed Colors
# ─────────────────────────────────────────────────────────────────────────────

COLORS = {
    "STOP_HIT":      0xE74C3C,   # red
    "STOP_LOSS":     0xE74C3C,   # red
    "TAKE_PROFIT":   0x2ECC71,   # green
    "TARGET_HIT":    0x2ECC71,   # green
    "DTE_WARNING":   0xE74C3C,   # red
    "EARNINGS":      0xF39C12,   # orange
    "TA_REVERSAL":   0xF39C12,   # orange
    "NEAR_STOP":     0xE67E22,   # dark orange
    "NEAR_TARGET":   0x27AE60,   # dark green
    "WATCH":         0xF1C40F,   # yellow
    "REPEAT_SIGNAL": 0x9B59B6,   # purple
    "NEW_POSITION":  0x3498DB,   # blue
    "HIGH":          0xE74C3C,
    "MEDIUM":        0xF1C40F,
    "LOW":           0x95A5A6,
}

EMOJIS = {
    "STOP_LOSS":     "🛑",
    "TAKE_PROFIT":   "🎯",
    "DTE_WARNING":   "⏰",
    "EARNINGS":      "📅",
    "TA_REVERSAL":   "📉",
    "NEAR_STOP":     "🟡",
    "NEAR_TARGET":   "🟢",
    "WATCH":         "👀",
    "REPEAT_SIGNAL": "🔁",
    "NEW_POSITION":  "🆕",
}

ACTIONS = {
    "STOP_LOSS":     "EXIT NOW — stop loss exceeded",
    "TAKE_PROFIT":   "TAKE PROFIT — target reached",
    "DTE_WARNING":   "CLOSE OPTION — theta accelerating",
    "EARNINGS":      "CONSIDER EXIT before earnings",
    "TA_REVERSAL":   "WATCH — technical sell signal",
    "NEAR_STOP":     "WATCH — approaching stop loss",
    "NEAR_TARGET":   "CONSIDER PARTIAL EXIT",
    "WATCH":         "MONITOR CLOSELY",
    "REPEAT_SIGNAL": "REPEATED SIGNAL — still unacted on",
    "NEW_POSITION":  "NEW POSITION DETECTED",
}


# ─────────────────────────────────────────────────────────────────────────────
# Webhook Management
# ─────────────────────────────────────────────────────────────────────────────

def save_webhook(user_id: str, webhook_url: str) -> bool:
    """Save Discord webhook URL to notification_config table."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                INSERT INTO notification_config (user_id, discord_webhook, discord_enabled, updated_at)
                VALUES (:uid, :url, TRUE, now())
                ON CONFLICT (user_id)
                DO UPDATE SET discord_webhook = EXCLUDED.discord_webhook,
                              discord_enabled = TRUE,
                              updated_at      = now()
            """), {"uid": user_id, "url": webhook_url})
        print(f"[Discord] Webhook saved for user {user_id[:8]}...")
        return True
    except Exception as e:
        print(f"[Discord] Failed to save webhook: {e}")
        return False


def get_webhook(user_id: str) -> str | None:
    """Get Discord webhook URL for user."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text("""
                SELECT discord_webhook FROM notification_config
                WHERE user_id = :uid AND discord_enabled = TRUE
            """), {"uid": user_id}).fetchone()
            return row.discord_webhook if row else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Embed Builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_embed(
    symbol: str,
    alert_type: str,
    urgency: str,
    message: str,
    pnl_pct: float | None = None,
    pnl_abs: float | None = None,
) -> dict:
    """Build a rich Discord embed for a trading alert."""
    emoji  = EMOJIS.get(alert_type, "📊")
    color  = COLORS.get(alert_type, COLORS.get(urgency, 0x95A5A6))
    action = ACTIONS.get(alert_type, "REVIEW POSITION")
    now_et = datetime.now(timezone.utc)

    # Title
    title = f"{emoji} {alert_type.replace('_', ' ')} — {symbol}"

    # Fields
    fields = []

    if pnl_pct is not None:
        pnl_str = f"{pnl_pct:+.1f}%"
        if pnl_abs is not None:
            pnl_str += f" (${abs(pnl_abs):,.0f} {'loss' if pnl_abs < 0 else 'gain'})"
        fields.append({"name": "P&L", "value": pnl_str, "inline": True})

    fields.append({"name": "Urgency", "value": urgency, "inline": True})
    fields.append({"name": "Action", "value": action, "inline": False})

    # Clean message (remove emoji prefixes already in title)
    clean_msg = message.replace("👤 Manual | ", "").replace("📊 Rec | ", "")
    if clean_msg and clean_msg != f"{symbol} — {action}":
        fields.append({"name": "Details", "value": clean_msg[:200], "inline": False})

    return {
        "title":       title,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "Trading Platform • Monitor Alert"},
        "timestamp":   now_et.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Send to Discord
# ─────────────────────────────────────────────────────────────────────────────

def send_discord(
    webhook_url: str,
    symbol: str,
    alert_type: str,
    urgency: str,
    message: str,
    pnl_pct: float | None = None,
    pnl_abs: float | None = None,
    mention: bool = False,
) -> bool:
    """
    Send a trading alert to Discord.
    mention=True adds @here for HIGH urgency (pings everyone in channel).
    Returns True if sent successfully.
    """
    try:
        embed   = _build_embed(symbol, alert_type, urgency, message, pnl_pct, pnl_abs)
        payload = {"embeds": [embed]}

        # @here mention for HIGH urgency so phone buzzes
        if mention and urgency == "HIGH":
            payload["content"] = "@here"

        r = requests.post(
            webhook_url, json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )

        if r.status_code == 204:
            print(f"[Discord] ✅ Sent: {alert_type} for {symbol}")
            return True
        else:
            print(f"[Discord] ❌ Failed {r.status_code}: {r.text[:100]}")
            return False

    except Exception as e:
        print(f"[Discord] ❌ Error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main Notification Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def notify(
    user_id: str,
    symbol: str,
    alert_type: str,
    urgency: str,
    message: str,
    pnl_pct: float | None = None,
    pnl_abs: float | None = None,
) -> bool:
    """
    Main notification function — called by position_monitor after fire_alert().

    Routing:
        HIGH   → Discord with @here mention (phone buzzes)
        MEDIUM → Discord without mention (silent badge)
        LOW    → Skip (no notification)

    Returns True if notification was sent.
    """
    # Skip LOW urgency — too noisy
    if urgency == "LOW":
        return False

    webhook = get_webhook(user_id)
    if not webhook:
        return False

    mention = (urgency == "HIGH")
    return send_discord(webhook, symbol, alert_type, urgency,
                        message, pnl_pct, pnl_abs, mention=mention)


def log_notification(
    user_id: str,
    symbol: str,
    alert_type: str,
    channel: str,
    success: bool,
) -> None:
    """Log notification attempt to DB for audit trail."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                INSERT INTO notification_log
                    (user_id, symbol, alert_type, channel, success, sent_at)
                VALUES (:uid, :sym, :atype, :ch, :ok, now())
            """), {
                "uid": user_id, "sym": symbol,
                "atype": alert_type, "ch": channel, "ok": success,
            })
    except Exception:
        pass  # Don't fail monitor if logging fails


def send_test_notification(user_id: str) -> dict:
    """Send a test alert to verify Discord is configured correctly."""
    webhook = get_webhook(user_id)
    if not webhook:
        return {"success": False, "error": "No Discord webhook configured. Run configure_discord() first."}

    success = send_discord(
        webhook_url = webhook,
        symbol      = "TEST",
        alert_type  = "TAKE_PROFIT",
        urgency     = "HIGH",
        message     = "This is a test alert from your Trading Platform. If you see this, notifications are working!",
        pnl_pct     = 42.0,
        pnl_abs     = 1234.0,
        mention     = False,
    )

    return {
        "success": success,
        "channel": "Discord",
        "message": "Test alert sent — check your #trading-alerts channel" if success
                   else "Failed to send — check webhook URL",
    }


def get_config(user_id: str) -> dict:
    """Get current notification configuration."""
    webhook = get_webhook(user_id)
    return {
        "discord_configured": webhook is not None,
        "webhook_preview":    webhook[:40] + "..." if webhook else None,
        "routing": {
            "HIGH":   "Discord with @here mention",
            "MEDIUM": "Discord without mention",
            "LOW":    "Silent — no notification",
        },
        "alert_types": list(ACTIONS.keys()),
    }