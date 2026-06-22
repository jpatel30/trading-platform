"""
C13 Position Monitor v2 (W13).

Two-tier monitoring:
    Recommended positions  → every 15 min + LLM enrichment on first detection
    Manual/unknown         → every 30 min + rule-based only
    New position detected  → immediate LLM: "not from our engine, here's analysis"

Flow:
    Every 15 min (recommended) / 30 min (manual):
        Fetch live positions from Webull
        Cache in portfolio_cache (SWR pattern for other tools)
        Detect NEW positions not previously seen → LLM analysis + alert
        Evaluate each position:
            - Recommended: compare vs target/stop from recommendation engine
            - Manual: compare vs rule-based thresholds
        Fire alerts with cooldown (no spam)
"""
import threading
import time
import json
from datetime import datetime, timedelta


# ─────────────────────────────────────────────────────────────────────────────
# Global State
# ─────────────────────────────────────────────────────────────────────────────

_monitors: dict[str, "PositionMonitor"] = {}


def get_monitor(user_id: str) -> "PositionMonitor":
    if user_id not in _monitors:
        _monitors[user_id] = PositionMonitor(user_id)
    return _monitors[user_id]


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio Cache
# ─────────────────────────────────────────────────────────────────────────────

def cache_portfolio(user_id: str, positions: list, balances: dict | None = None) -> None:
    """Cache positions in DB for instant reads by other tools."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                INSERT INTO portfolio_cache (user_id, positions, balances, updated_at)
                VALUES (:uid, :pos, :bal, now())
                ON CONFLICT (user_id)
                DO UPDATE SET positions=EXCLUDED.positions,
                              balances=EXCLUDED.balances,
                              updated_at=now()
            """), {"uid": user_id, "pos": json.dumps(positions),
                   "bal": json.dumps(balances) if balances else None})
    except Exception as e:
        print(f"[Monitor] Cache write failed: {e}")


def get_cached_portfolio(user_id: str) -> dict | None:
    """Get cached positions. Returns None if not found."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text(
                "SELECT positions, balances, updated_at FROM portfolio_cache WHERE user_id=:uid"
            ), {"uid": user_id}).fetchone()
            if not row:
                return None
            age = (datetime.now(row.updated_at.tzinfo) - row.updated_at).seconds // 60
            return {
                "positions":   json.loads(row.positions),
                "balances":    json.loads(row.balances) if row.balances else None,
                "cached_at":   row.updated_at.isoformat(),
                "age_minutes": age,
                "is_stale":    age > 35,
            }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Tracked Positions
# ─────────────────────────────────────────────────────────────────────────────

def get_tracked_symbols(user_id: str) -> dict[str, dict]:
    """
    Get all active tracked positions keyed by symbol.
    Returns {symbol: {source, target_pct, stop_pct, check_interval_min, ...}}
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT symbol, source, target_pct, stop_pct,
                       target_price, stop_price, entry_price,
                       check_interval_min, entry_date
                FROM tracked_positions
                WHERE user_id=:uid AND is_active=TRUE
            """), {"uid": user_id}).fetchall()
            return {r.symbol: dict(r._mapping) for r in rows}
    except Exception:
        return {}


def add_tracked_position(
    user_id: str,
    symbol: str,
    source: str = "manual",
    entry_price: float | None = None,
    qty: int | None = None,
    target_pct: float | None = None,
    stop_pct: float | None = None,
    llm_entry_note: str | None = None,
) -> bool:
    """Add or update a tracked position."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        interval = 15 if source == "recommendation" else 30
        with get_session() as s:
            s.execute(text("""
                INSERT INTO tracked_positions
                    (user_id, symbol, source, entry_date, entry_price, qty,
                     target_pct, stop_pct, check_interval_min, llm_entry_note)
                VALUES (:uid, :sym, :src, CURRENT_DATE, :ep, :qty,
                        :tgt, :stp, :interval, :llm)
                ON CONFLICT (user_id, symbol, entry_date)
                DO UPDATE SET
                    source             = EXCLUDED.source,
                    entry_price        = EXCLUDED.entry_price,
                    target_pct         = EXCLUDED.target_pct,
                    stop_pct           = EXCLUDED.stop_pct,
                    check_interval_min = EXCLUDED.check_interval_min,
                    llm_entry_note     = EXCLUDED.llm_entry_note,
                    is_active          = TRUE
            """), {
                "uid": user_id, "sym": symbol, "src": source,
                "ep": entry_price, "qty": qty,
                "tgt": target_pct or 20.0,
                "stp": stop_pct or -40.0,
                "interval": interval,
                "llm": llm_entry_note,
            })
        return True
    except Exception as e:
        print(f"[Monitor] add_tracked failed: {e}")
        return False


def close_tracked_position(user_id: str, symbol: str, exit_reason: str = "MANUAL") -> bool:
    """Mark a tracked position as closed."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                UPDATE tracked_positions
                SET is_active=FALSE, exit_date=CURRENT_DATE, exit_reason=:reason
                WHERE user_id=:uid AND symbol=:sym AND is_active=TRUE
            """), {"uid": user_id, "sym": symbol, "reason": exit_reason})
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Alert Management
# ─────────────────────────────────────────────────────────────────────────────

def _check_cooldown(user_id: str, symbol: str, alert_type: str, cooldown_min: int) -> bool:
    """True = can fire (not in cooldown)."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text("""
                SELECT 1 FROM position_alerts
                WHERE user_id=:uid AND symbol=:sym AND alert_type=:at
                  AND triggered_at >= now() - :mins * interval '1 minute'
                LIMIT 1
            """), {"uid": user_id, "sym": symbol, "at": alert_type, "mins": cooldown_min}).fetchone()
            return row is None
    except Exception:
        return True


def mute_alerts(user_id: str, symbol: str | None = None, hours: int | None = None) -> dict:
    """
    Mute alerts globally or for a specific symbol.
    Args:
        symbol: specific ticker to mute (None = mute all)
        hours:  mute for X hours (None = until manually unmuted)
    """
    from sqlalchemy import text
    from app.db.session import get_session

    until = (datetime.now() + timedelta(hours=hours)) if hours else None
    until_str = until.isoformat() if until else "permanently"

    if symbol:
        # Mute specific symbol
        with get_session() as s:
            s.execute(text("""
                INSERT INTO muted_symbols (user_id, symbol, muted_until)
                VALUES (:uid, :sym, :until)
                ON CONFLICT (user_id, symbol)
                DO UPDATE SET muted_at=now(), muted_until=EXCLUDED.muted_until
            """), {"uid": user_id, "sym": symbol.upper(), "until": until})
        return {"muted": True, "symbol": symbol.upper(), "until": until_str}
    else:
        # Mute all alerts globally
        with get_session() as s:
            s.execute(text("""
                INSERT INTO monitor_config (user_id, alerts_muted, muted_until)
                VALUES (:uid, TRUE, :until)
                ON CONFLICT (user_id)
                DO UPDATE SET alerts_muted=TRUE, muted_until=EXCLUDED.muted_until
            """), {"uid": user_id, "until": until})
        return {"muted": True, "symbol": "ALL", "until": until_str}


def unmute_alerts(user_id: str, symbol: str | None = None) -> dict:
    """
    Re-enable alerts globally or for a specific symbol.
    """
    from sqlalchemy import text
    from app.db.session import get_session

    if symbol:
        with get_session() as s:
            s.execute(text("""
                DELETE FROM muted_symbols WHERE user_id=:uid AND symbol=:sym
            """), {"uid": user_id, "sym": symbol.upper()})
        return {"unmuted": True, "symbol": symbol.upper()}
    else:
        with get_session() as s:
            s.execute(text("""
                UPDATE monitor_config
                SET alerts_muted=FALSE, muted_until=NULL
                WHERE user_id=:uid
            """), {"uid": user_id})
        return {"unmuted": True, "symbol": "ALL"}


def _is_muted(user_id: str, symbol: str) -> bool:
    """
    Check if alerts are muted — global or symbol-specific.
    Automatically clears expired mutes.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            # Check global mute
            cfg = s.execute(text("""
                SELECT alerts_muted, muted_until FROM monitor_config
                WHERE user_id=:uid
            """), {"uid": user_id}).fetchone()

            if cfg and cfg.alerts_muted:
                if cfg.muted_until is None:
                    return True  # permanent global mute
                if datetime.now(cfg.muted_until.tzinfo) < cfg.muted_until:
                    return True  # still within mute window
                # Expired — auto-clear
                s.execute(text("""
                    UPDATE monitor_config SET alerts_muted=FALSE, muted_until=NULL
                    WHERE user_id=:uid
                """), {"uid": user_id})

            # Check symbol-specific mute
            row = s.execute(text("""
                SELECT muted_until FROM muted_symbols
                WHERE user_id=:uid AND symbol=:sym
            """), {"uid": user_id, "sym": symbol.upper()}).fetchone()

            if row:
                if row.muted_until is None:
                    return True  # permanent symbol mute
                if datetime.now(row.muted_until.tzinfo) < row.muted_until:
                    return True  # still within window
                # Expired — auto-clear
                s.execute(text("""
                    DELETE FROM muted_symbols WHERE user_id=:uid AND symbol=:sym
                """), {"uid": user_id, "sym": symbol.upper()})

            return False
    except Exception:
        return False  # default: don't mute on error


def get_mute_status(user_id: str) -> dict:
    """Show current mute configuration."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            cfg = s.execute(text("""
                SELECT alerts_muted, muted_until FROM monitor_config WHERE user_id=:uid
            """), {"uid": user_id}).fetchone()

            muted_syms = s.execute(text("""
                SELECT symbol, muted_until FROM muted_symbols WHERE user_id=:uid
            """), {"uid": user_id}).fetchall()

        return {
            "global_muted":  cfg.alerts_muted if cfg else False,
            "global_until":  cfg.muted_until.isoformat() if cfg and cfg.muted_until else None,
            "muted_symbols": [
                {"symbol": r.symbol,
                 "until": r.muted_until.isoformat() if r.muted_until else "permanent"}
                for r in muted_syms
            ],
        }
    except Exception:
        return {"global_muted": False, "muted_symbols": []}


def fire_alert(user_id: str, symbol: str, alert_type: str, urgency: str,
               message: str, pnl_pct: float | None = None,
               pnl_abs: float | None = None, cooldown_min: int = 60) -> bool:
    """Insert alert if not muted and not in cooldown. Returns True if fired."""
    # Check mute FIRST — respects user preference
    if _is_muted(user_id, symbol):
        return False

    if not _check_cooldown(user_id, symbol, alert_type, cooldown_min):
        return False

    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                INSERT INTO position_alerts
                    (user_id, symbol, alert_type, urgency, message, pnl_pct, pnl_abs)
                VALUES (:uid, :sym, :at, :urg, :msg, :pct, :abs)
            """), {"uid": user_id, "sym": symbol, "at": alert_type,
                   "urg": urgency, "msg": message, "pct": pnl_pct, "abs": pnl_abs})

        # Send Discord notification
        try:
            from app.notifications.discord import notify
            notify(user_id, symbol, alert_type, urgency, message, pnl_pct, pnl_abs)
        except Exception as ne:
            print(f"[Monitor] Notification failed: {ne}")
        return True
    except Exception as e:
        print(f"[Monitor] Alert insert failed: {e}")
        return False


def get_active_alerts(user_id: str, limit: int = 20) -> list[dict]:
    """Unread alerts sorted by urgency then time."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT id, symbol, alert_type, urgency, message,
                       pnl_pct, pnl_abs, triggered_at, read_at
                FROM position_alerts
                WHERE user_id=:uid AND dismissed=FALSE
                ORDER BY
                    CASE urgency WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                    triggered_at DESC
                LIMIT :lim
            """), {"uid": user_id, "lim": limit}).fetchall()
            return [{"id": str(r.id), "symbol": r.symbol, "alert_type": r.alert_type,
                     "urgency": r.urgency, "message": r.message,
                     "pnl_pct": float(r.pnl_pct) if r.pnl_pct else None,
                     "pnl_abs": float(r.pnl_abs) if r.pnl_abs else None,
                     "triggered_at": r.triggered_at.isoformat(),
                     "read": r.read_at is not None} for r in rows]
    except Exception:
        return []


def dismiss_alert(user_id: str, alert_id: str) -> bool:
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                UPDATE position_alerts SET dismissed=TRUE, read_at=now()
                WHERE id=:aid AND user_id=:uid
            """), {"aid": alert_id, "uid": user_id})
        return True
    except Exception:
        return False


def dismiss_all_alerts(user_id: str) -> int:
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            r = s.execute(text("""
                UPDATE position_alerts SET dismissed=TRUE, read_at=now()
                WHERE user_id=:uid AND dismissed=FALSE
            """), {"uid": user_id})
            return r.rowcount
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# LLM Enrichment (new positions only)
# ─────────────────────────────────────────────────────────────────────────────

def _llm_analyze_new_position(symbol: str, pnl_pct: float, source: str) -> str:
    """Quick LLM sentence for newly detected position."""
    try:
        from app.llm.service import _call_ollama
        origin = "from our recommendation engine" if source == "recommendation" \
                 else "NOT from our recommendation engine — manually opened"
        r = _call_ollama(
            prompt=f"{symbol} new position detected, {pnl_pct:+.1f}% P&L, {origin}. "
                   f"One sentence: what should the trader know right now?",
            system="Expert trader. One sentence only. Actionable.",
            max_tokens=50,
        )
        return r.strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Position Monitor
# ─────────────────────────────────────────────────────────────────────────────

class PositionMonitor:
    """
    Background monitor with two-tier polling:
        Recommended positions → every 15 min (tighter, we know targets)
        Manual positions      → every 30 min (looser, rule-based)
        New positions         → immediate LLM analysis on detection
    """

    RECOMMENDED_INTERVAL = 15 * 60   # 15 minutes in seconds
    MANUAL_INTERVAL      = 30 * 60   # 30 minutes in seconds

    def __init__(self, user_id: str):
        self.user_id        = user_id
        self.running        = False
        self.thread         = None
        self.last_check     = None
        self.last_error     = None
        self.total_checks   = 0
        self.alerts_fired   = 0
        self._known_symbols: set[str] = set()   # tracks what we've seen before

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def start(self) -> dict:
        if self.running:
            return {"status": "already_running", "since": str(self.last_check)}
        self.running = True
        self.thread  = threading.Thread(
            target=self._poll_loop, daemon=True,
            name=f"monitor-{self.user_id[:8]}")
        self.thread.start()
        self._save_config(is_active=True)
        print(f"[Monitor] Started — recommended:15min / manual:30min")
        return {"status": "started",
                "recommended_interval_min": 15,
                "manual_interval_min": 30}

    def stop(self) -> dict:
        self.running = False
        self._save_config(is_active=False)
        return {"status": "stopped", "total_checks": self.total_checks,
                "alerts_fired": self.alerts_fired}

    def status(self) -> dict:
        alerts = get_active_alerts(self.user_id, limit=5)
        return {
            "running":          self.running,
            "last_check":       self.last_check.isoformat() if self.last_check else None,
            "last_error":       self.last_error,
            "intervals":        {"recommended_min": 15, "manual_min": 30},
            "total_checks":     self.total_checks,
            "alerts_fired":     self.alerts_fired,
            "pending_alerts":   len([a for a in alerts if not a["read"]]),
            "recent_alerts":    alerts[:3],
        }

    # ── Poll Loop ─────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Main loop: run at RECOMMENDED_INTERVAL, skip non-recommended every other cycle."""
        cycle = 0
        while self.running:
            try:
                if self._market_open():
                    cycle += 1
                    # Every cycle = check recommended (15 min)
                    # Every 2nd cycle = also check manual (30 min)
                    check_manual = (cycle % 2 == 0)
                    fired = self._check_positions(check_manual=check_manual)
                    self.alerts_fired += fired
                    self.total_checks += 1
                    self.last_check    = datetime.now()
                    self.last_error    = None
                    self._save_config(last_check_at=self.last_check,
                                      total_checks=self.total_checks,
                                      total_alerts_fired=self.alerts_fired)
            except Exception as e:
                self.last_error = str(e)
                print(f"[Monitor] Poll error: {e}")

            time.sleep(self.RECOMMENDED_INTERVAL)

    def _market_open(self) -> bool:
        """True during US market hours + 30min buffer."""
        try:
            import pytz
            from datetime import time as dtime
            from app.scanner.quick_scan import us_market_holidays
            et  = pytz.timezone("America/New_York")
            now = datetime.now(et)
            if now.weekday() >= 5: return False
            if now.date() in us_market_holidays(now.year): return False
            t = now.time()
            return dtime(9, 0) <= t <= dtime(16, 30)
        except Exception:
            return True

    # ── Core Check ────────────────────────────────────────────────────────────

    def _check_positions(self, check_manual: bool = True) -> int:
        """
        Fetch positions, detect new ones, evaluate against targets.
        check_manual=False → only evaluate recommended positions (15 min cycle).
        check_manual=True  → evaluate all (30 min cycle).
        """
        from app.broker.webull_connector import WebullConnector
        from app.broker.sell_signals import evaluate_sell_signals

        wb        = WebullConnector(self.user_id)
        positions = wb.get_positions()
        if not positions:
            return 0

        # Cache for other tools
        try: bal = wb.get_balance()
        except: bal = None
        cache_portfolio(self.user_id, positions, bal)

        # Load tracked position metadata
        tracked = get_tracked_symbols(self.user_id)

        # Detect NEW positions not seen before
        current_symbols = {p["symbol"] for p in positions}
        new_symbols     = current_symbols - self._known_symbols
        if self._known_symbols:   # skip on very first run
            for sym in new_symbols:
                self._handle_new_position(sym, positions, tracked)
        self._known_symbols = current_symbols

        # Evaluate signals
        signals     = evaluate_sell_signals(positions)
        alerts_fired = 0

        for signal in signals:
            symbol = signal["symbol"]
            source = tracked.get(symbol, {}).get("source", "manual")

            # Skip manual positions on 15-min cycle
            if not check_manual and source != "recommendation":
                continue

            pnl_pct = signal["pnl_pct"]
            pnl_abs = signal["pnl"]

            for rule in signal["signals"]:
                atype, urgency, msg, cooldown = self._classify_rule(
                    rule, symbol, pnl_pct, pnl_abs, source
                )
                fired = fire_alert(
                    self.user_id, symbol, atype, urgency, msg,
                    pnl_pct, pnl_abs, cooldown
                )
                if fired:
                    alerts_fired += 1
                    print(f"[Monitor] 🔔 {urgency} [{source}] {msg[:70]}")

        return alerts_fired

    def _classify_rule(
        self, rule: str, symbol: str, pnl_pct: float, pnl_abs: float, source: str
    ) -> tuple[str, str, str, int]:
        """Returns (alert_type, urgency, message, cooldown_minutes)."""
        origin = "📊 Recommended" if source == "recommendation" else "👤 Manual"
        base   = f"{origin} | {symbol} {pnl_pct:+.1f}%"

        if "STOP LOSS" in rule:
            return ("STOP_LOSS", "HIGH",
                    f"{base} — Stop loss hit (${pnl_abs:,.0f}). Exit now.", 120)
        if "TAKE PROFIT" in rule:
            return ("TAKE_PROFIT", "HIGH",
                    f"{base} — Profit target hit (${pnl_abs:,.0f}). Review exit.", 120)
        if "EARNINGS" in rule:
            return ("EARNINGS", "MEDIUM",
                    f"{base} — {rule}. Consider exiting before earnings.", 240)
        if "DTE" in rule:
            return ("DTE_WARNING", "HIGH",
                    f"{base} — {rule}. Theta accelerating.", 60)
        if "TA" in rule:
            return ("TA_REVERSAL", "MEDIUM",
                    f"{base} — Technical sell signal detected.", 240)
        if "LOSS WATCH" in rule:
            return ("WATCH", "MEDIUM",
                    f"{base} — Approaching stop loss. Watch closely.", 120)
        if "REPEAT" in rule:
            return ("REPEAT_SIGNAL", "MEDIUM",
                    f"{base} — {rule}", 480)

        return ("WATCH", "LOW", f"{base} — {rule}", 240)

    def _handle_new_position(self, symbol: str, positions: list, tracked: dict) -> None:
        """
        Called when a position appears that wasn't there before.
        Runs LLM analysis and fires NEW_POSITION alert.
        """
        pos    = next((p for p in positions if p["symbol"] == symbol), {})
        pnl    = float(pos.get("unrealized_profit_loss_rate", 0)) * 100
        source = tracked.get(symbol, {}).get("source", "manual")

        print(f"[Monitor] 🆕 New position detected: {symbol} ({source})")

        # Quick LLM sentence
        llm_note = _llm_analyze_new_position(symbol, pnl, source)

        # Auto-add to tracked if not already there
        if symbol not in tracked:
            add_tracked_position(
                self.user_id, symbol, source="manual",
                entry_price=float(pos.get("unit_cost", 0)),
                llm_entry_note=llm_note,
            )

        origin_label = "from our recommendation engine" if source == "recommendation" \
                       else "NOT from our recommendation engine"
        msg = f"New position: {symbol} ({pnl:+.1f}%) — {origin_label}. {llm_note}"

        fire_alert(self.user_id, symbol, "NEW_POSITION", "MEDIUM",
                   msg, pnl, cooldown_min=9999)  # only fire once

    # ── Config helpers ────────────────────────────────────────────────────────

    def _save_config(self, **kwargs) -> None:
        try:
            from sqlalchemy import text
            from app.db.session import get_session
            cols    = list(kwargs.keys())
            with get_session() as s:
                s.execute(text("""
                    INSERT INTO monitor_config (user_id, {cols})
                    VALUES (:uid, {vals})
                    ON CONFLICT (user_id) DO UPDATE SET {updates}
                """.format(
                    cols    = ", ".join(cols),
                    vals    = ", ".join(f":{k}" for k in cols),
                    updates = ", ".join(f"{k}=EXCLUDED.{k}" for k in cols),
                )), {"uid": self.user_id, **kwargs})
        except Exception:
            pass