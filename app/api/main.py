"""
StockBros FastAPI Backend.

REST API wrapper around all MCP tools.
Runs on :8000, called by Next.js dashboard on :3000.

Auth: JWT tokens using existing user/invite system.
CORS: localhost:3000 (dev) + your domain (prod).

Start:
    uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload
"""
import os
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

try:
    from jose import JWTError, jwt
except ImportError:
    raise ImportError("Run: pip install python-jose[cryptography]")

# ─────────────────────────────────────────────────────────────────────────────
# App + CORS
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "StockBros Trading API",
    description = "REST API for StockBros trading intelligence dashboard",
    version     = "1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        os.getenv("FRONTEND_URL", "http://localhost:3000"),
    ],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# JWT Auth
# ─────────────────────────────────────────────────────────────────────────────

SECRET_KEY = os.getenv("JWT_SECRET", os.getenv("ENCRYPTION_KEY", "dev-secret-change-in-prod"))
ALGORITHM  = "HS256"
TOKEN_EXPIRE_DAYS = 30

security = HTTPBearer()


def create_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    try:
        payload  = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id  = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response Models
# ─────────────────────────────────────────────────────────────────────────────

class InviteLoginRequest(BaseModel):
    invite_code: str
    display_name: Optional[str] = None

class LoginResponse(BaseModel):
    token:        str
    user_id:      str
    display_name: str
    email:        Optional[str] = None

class AddToWatchlistRequest(BaseModel):
    ticker: str

class ConfirmExecutionRequest(BaseModel):
    symbol:      str
    entry_price: float
    qty:         int

class LogOutcomeRequest(BaseModel):
    symbol:      str
    exit_price:  float
    exit_reason: str = "MANUAL"

class ConfigureDiscordRequest(BaseModel):
    webhook_url: str

class InvalidateRecRequest(BaseModel):
    ticker: str
    reason: str = "Manual invalidation"

class HorizonRecRequest(BaseModel):
    ticker:  str
    horizon: str = "1m"
    budget:  float = 2000.0


# ─────────────────────────────────────────────────────────────────────────────
# Auth Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/auth/login", response_model=LoginResponse, tags=["Auth"])
async def login_with_invite(req: InviteLoginRequest):
    """Login or register with an invite code."""
    try:
        from sqlalchemy import text
        from app.db.session import get_session

        with get_session() as s:
            # Check invite code
            invite = s.execute(text("""
                SELECT id, invited_by, email, status
                FROM invites
                WHERE invite_code = :code AND status = 'pending'
                  AND (expires_at IS NULL OR expires_at > now())
            """), {"code": req.invite_code}).fetchone()

            if not invite:
                raise HTTPException(status_code=400, detail="Invalid or expired invite code")

            # Check if user already exists for this invite
            existing = s.execute(text("""
                SELECT id, display_name, email FROM users
                WHERE invited_by = :invited_by
                LIMIT 1
            """), {"invited_by": invite.invited_by}).fetchone()

            if existing:
                user_id      = str(existing.id)
                display_name = existing.display_name
                email        = existing.email
            else:
                # Create new user
                display_name = req.display_name or f"Trader_{req.invite_code[:6]}"
                # Use invite email — users.email is NOT NULL
                user_email = invite.email or f"{req.invite_code.lower()}@stockbros.app"
                row = s.execute(text("""
                    INSERT INTO users (display_name, email, invited_by, is_active)
                    VALUES (:name, :email, :invited_by, TRUE)
                    RETURNING id, display_name
                """), {"name": display_name, "email": user_email,
                       "invited_by": invite.invited_by}).fetchone()
                user_id = str(row.id)
                email   = user_email

                # Create default user_profile
                s.execute(text("""
                    INSERT INTO user_profiles (user_id, risk_tolerance)
                    VALUES (:uid, 'moderate')
                    ON CONFLICT DO NOTHING
                """), {"uid": user_id})

                # Mark invite as accepted
                s.execute(text("""
                    UPDATE invites SET status='accepted', accepted_at=now()
                    WHERE id = :id
                """), {"id": invite.id})

        token = create_token(user_id, email or "")
        return LoginResponse(
            token        = token,
            user_id      = user_id,
            display_name = display_name,
            email        = email,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/auth/me", tags=["Auth"])
async def get_me(user_id: str = Depends(get_current_user)):
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text(
                "SELECT display_name, email, created_at FROM users WHERE id=:uid"
            ), {"uid": user_id}).fetchone()
        return {"user_id": user_id, "display_name": row.display_name if row else "Trader"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/health", tags=["System"])
async def health():
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        models = [m["name"] for m in r.json().get("models", [])]
        llm_ok = True
    except Exception:
        models = []
        llm_ok = False

    return {
        "status":    "ok" if db_ok else "degraded",
        "db":        db_ok,
        "llm":       llm_ok,
        "llm_models": models,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/market/status", tags=["Market"])
async def market_status():
    from app.scanner.quick_scan import get_last_trading_date
    return {
        "last_trading_date": get_last_trading_date(),
        "timestamp":         datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/portfolio", tags=["Portfolio"])
async def get_portfolio(
    live: bool = False,
    user_id: str = Depends(get_current_user)
):
    try:
        from app.broker.sell_signals import get_portfolio_pnl_summary
        from app.broker.active_bets import get_active_bets
        from app.monitor.position_monitor import get_cached_portfolio

        # Use cache for instant response (updated every 15-30 min by monitor)
        if not live:
            cache = get_cached_portfolio(user_id)
            if cache and not cache.get("is_stale"):
                positions = cache["positions"]
                balances  = cache.get("balances") or {}
                pnl       = get_portfolio_pnl_summary(positions, None)
                bets      = get_active_bets(positions, user_id=user_id)
                return {
                    "positions": positions,
                    "balances":  balances,
                    "pnl":       pnl,
                    "bets":      bets,
                    "source":    "cache",
                    "cached_at": cache.get("cached_at"),
                    "age_minutes": cache.get("age_minutes"),
                }

        # Live fetch from Webull
        from app.broker.factory import get_broker
        broker    = get_broker(user_id)
        positions = broker.get_positions()
        balances  = broker.get_balances()
        pnl       = get_portfolio_pnl_summary(positions, None)
        bets      = get_active_bets(positions, user_id=user_id)
        return {
            "positions": positions,
            "balances":  balances,
            "pnl":       pnl,
            "bets":      bets,
            "source":    "live",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/portfolio/pnl", tags=["Portfolio"])
async def get_pnl(user_id: str = Depends(get_current_user)):
    try:
        from app.broker.factory import get_broker
        from app.broker.sell_signals import get_portfolio_pnl_summary
        broker    = get_broker(user_id)
        positions = broker.get_positions()
        return get_portfolio_pnl_summary(positions, None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/portfolio/active-bets", tags=["Portfolio"])
async def get_active_bets_api(user_id: str = Depends(get_current_user)):
    try:
        from app.broker.factory import get_broker
        from app.broker.active_bets import get_active_bets
        broker    = get_broker(user_id)
        positions = broker.get_positions()
        return get_active_bets(positions, user_id=user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Recommendations
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/recommendations/daily", tags=["Recommendations"])
async def get_daily_recs(
    force_refresh: bool = False,
    user_id: str = Depends(get_current_user)
):
    try:
        from app.recommendations.daily_engine import (
            run_daily_recommendations, get_active_recommendations
        )
        # Always check cache first — instant response
        cached = get_active_recommendations(user_id)
        if cached:
            return {
                "recommendations": cached,
                "source":          "cached",
                "count":           len(cached),
            }
        # No cache and no force_refresh — tell dashboard to prompt user
        if not force_refresh:
            return {
                "recommendations": [],
                "source":          "empty",
                "message":         "No recommendations yet today. Click Scan to generate.",
                "needs_scan":      True,
            }
        # User explicitly clicked Scan — run full analysis
        return run_daily_recommendations(user_id, force_refresh=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/recommendations/invalidate", tags=["Recommendations"])
async def invalidate_rec(
    req: InvalidateRecRequest,
    user_id: str = Depends(get_current_user)
):
    try:
        from app.recommendations.daily_engine import invalidate_recommendation
        success = invalidate_recommendation(user_id, req.ticker, req.reason)
        return {"success": success, "ticker": req.ticker}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/recommendations/history", tags=["Recommendations"])
async def get_rec_history(
    days_back: int = 7,
    user_id: str = Depends(get_current_user)
):
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            rows = s.execute(text("""
                SELECT ticker, date, direction, conviction_score,
                       conviction_tier, thesis, target_pct, stop_pct,
                       status, invalidated_reason, strategy, risk_reward
                FROM daily_recommendations
                WHERE user_id = :uid AND date >= CURRENT_DATE - :days
                ORDER BY date DESC, conviction_score DESC
            """), {"uid": user_id, "days": days_back}).fetchall()
        return [dict(r._mapping) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/recommendations/horizon", tags=["Recommendations"])
async def get_horizon_rec(
    req: HorizonRecRequest,
    user_id: str = Depends(get_current_user)
):
    try:
        from app.recommendations.horizon_engine import get_horizon_recommendation
        return get_horizon_recommendation(req.ticker, req.horizon, req.budget, user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/alerts", tags=["Alerts"])
async def get_alerts(
    limit: int = 20,
    user_id: str = Depends(get_current_user)
):
    from app.monitor.position_monitor import get_active_alerts
    return get_active_alerts(user_id, limit=limit)


@app.post("/api/alerts/{alert_id}/dismiss", tags=["Alerts"])
async def dismiss_one(
    alert_id: str,
    user_id: str = Depends(get_current_user)
):
    from app.monitor.position_monitor import dismiss_alert
    return {"success": dismiss_alert(user_id, alert_id)}


@app.post("/api/alerts/dismiss-all", tags=["Alerts"])
async def dismiss_all(user_id: str = Depends(get_current_user)):
    from app.monitor.position_monitor import dismiss_all_alerts
    count = dismiss_all_alerts(user_id)
    return {"dismissed": count}


@app.post("/api/monitor/start", tags=["Monitor"])
async def start_monitor(user_id: str = Depends(get_current_user)):
    from app.monitor.position_monitor import get_monitor
    return get_monitor(user_id).start()


@app.post("/api/monitor/stop", tags=["Monitor"])
async def stop_monitor(user_id: str = Depends(get_current_user)):
    from app.monitor.position_monitor import get_monitor
    return get_monitor(user_id).stop()


@app.get("/api/monitor/status", tags=["Monitor"])
async def monitor_status(user_id: str = Depends(get_current_user)):
    from app.monitor.position_monitor import get_monitor
    return get_monitor(user_id).status()


# ─────────────────────────────────────────────────────────────────────────────
# Watchlist
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/watchlist", tags=["Watchlist"])
async def get_watchlist(user_id: str = Depends(get_current_user)):
    from app.broker.watchlist_sync import get_db_watchlist
    return {"tickers": get_db_watchlist(user_id)}


@app.post("/api/watchlist/add", tags=["Watchlist"])
async def add_ticker(
    req: AddToWatchlistRequest,
    user_id: str = Depends(get_current_user)
):
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                INSERT INTO user_watchlist (user_id, ticker)
                VALUES (:uid, :ticker)
                ON CONFLICT (user_id, ticker) DO NOTHING
            """), {"uid": user_id, "ticker": req.ticker.upper()})
        return {"added": True, "ticker": req.ticker.upper()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/watchlist/{ticker}", tags=["Watchlist"])
async def remove_ticker(
    ticker: str,
    user_id: str = Depends(get_current_user)
):
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            s.execute(text("""
                DELETE FROM user_watchlist
                WHERE user_id=:uid AND ticker=:ticker
            """), {"uid": user_id, "ticker": ticker.upper()})
        return {"removed": True, "ticker": ticker.upper()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Sell Signals
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/sell-signals", tags=["Signals"])
async def get_sell_signals(user_id: str = Depends(get_current_user)):
    try:
        from app.broker.factory import get_broker
        from app.broker.sell_signals import evaluate_sell_signals
        broker    = get_broker(user_id)
        positions = broker.get_positions()
        return evaluate_sell_signals(positions)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Learning
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/learning/report", tags=["Learning"])
async def learning_report(user_id: str = Depends(get_current_user)):
    from app.learning.engine import get_learning_report
    return get_learning_report(user_id)


@app.get("/api/learning/backtest", tags=["Learning"])
async def backtest(user_id: str = Depends(get_current_user)):
    from app.recommendations.backtester import run_full_backtest
    return run_full_backtest(user_id)


# ─────────────────────────────────────────────────────────────────────────────
# Execution Tracking
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/execution/confirm", tags=["Execution"])
async def confirm_exec(
    req: ConfirmExecutionRequest,
    user_id: str = Depends(get_current_user)
):
    from app.learning.prediction_tracker import confirm_execution
    return confirm_execution(user_id, req.symbol, req.entry_price, req.qty)


@app.post("/api/execution/outcome", tags=["Execution"])
async def log_exec_outcome(
    req: LogOutcomeRequest,
    user_id: str = Depends(get_current_user)
):
    from app.learning.prediction_tracker import log_outcome
    return log_outcome(user_id, req.symbol, req.exit_price, req.exit_reason)


# ─────────────────────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/notifications/config", tags=["Notifications"])
async def notif_config(user_id: str = Depends(get_current_user)):
    from app.notifications.discord import get_config
    return get_config(user_id)


@app.post("/api/notifications/discord", tags=["Notifications"])
async def configure_discord(
    req: ConfigureDiscordRequest,
    user_id: str = Depends(get_current_user)
):
    from app.notifications.discord import save_webhook, send_test_notification
    saved = save_webhook(user_id, req.webhook_url)
    if not saved:
        raise HTTPException(status_code=500, detail="Failed to save webhook")
    return send_test_notification(user_id)


# ─────────────────────────────────────────────────────────────────────────────
# Brokers
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/brokers", tags=["Brokers"])
async def list_brokers():
    from app.broker.factory import list_supported_brokers
    return list_supported_brokers()


@app.get("/api/brokers/active", tags=["Brokers"])
async def active_broker(user_id: str = Depends(get_current_user)):
    from app.broker.factory import get_active_broker_name
    return {"broker": get_active_broker_name(user_id)}
