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

from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.broker.base import BrokerNotConnectedError

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
# Broker-not-connected — centralized, not per-endpoint
# ─────────────────────────────────────────────────────────────────────────────
#
# Broker connection (Webull) is admin-only by design (see ARCHITECTURE.md's
# "Broker Connection Is Optional") - every non-admin user hits
# BrokerNotConnectedError on every broker-touching endpoint, always, not as
# an edge case. A single handler here means a new call site added later
# can't reintroduce an uncaught 500 the way four separate per-endpoint
# try/excepts already did - the same class of "same fix needed in N places"
# duplication (flow_scoring, excluded_from_stats) already cleaned up
# elsewhere this session. 200, not 500: this is an expected, normal state
# for most users, not a server error - shaped per endpoint with a
# `no_broker` flag so the frontend can distinguish it from "connected but
# genuinely zero positions/signals" unambiguously.
#
# Each broker-touching endpoint must let BrokerNotConnectedError propagate
# past its own try/except (via `except BrokerNotConnectedError: raise`
# before any blanket `except Exception`) for this handler to ever see it.
@app.exception_handler(BrokerNotConnectedError)
async def broker_not_connected_handler(request: Request, exc: BrokerNotConnectedError):
    path = request.url.path
    if path == "/api/sell-signals":
        body = {"no_broker": True, "signals": [], "pnl": {}}
    elif path == "/api/portfolio/check-fills":
        body = {"no_broker": True, "new_fills": [], "positions_count": 0, "recs_count": 0}
    else:
        # /api/portfolio, /api/portfolio/active-bets, and any future
        # broker-touching endpoint default to the full portfolio shape.
        body = {
            "no_broker": True, "positions": [], "balances": {}, "pnl": {},
            "bets": [], "source": "no_broker",
        }
    return JSONResponse(status_code=200, content=body)


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


def _require_admin(user_id: str) -> None:
    """
    Raises 403 if user_id is not an admin. Shared by get_current_admin_user_id
    (for endpoints that are admin-only outright) and by any endpoint that
    only conditionally gates a specific param on admin (e.g. history-grouped's
    all_users) - one place to check users.is_admin instead of re-implementing
    the query at each new admin-gated spot.
    """
    from sqlalchemy import text
    from app.db.session import get_session
    with get_session() as s:
        row = s.execute(text("SELECT is_admin FROM users WHERE id=:uid"), {"uid": user_id}).fetchone()
    if not row or not row.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


def get_current_admin_user_id(user_id: str = Depends(get_current_user)) -> str:
    """Drop-in Depends() replacement for get_current_user on endpoints
    that are admin-only outright (403s otherwise)."""
    _require_admin(user_id)
    return user_id


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
    mcp_api_key:  Optional[str] = None  # plaintext, only ever present on account creation

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

@app.get("/api/scan/status", tags=["Recommendations"])
async def scan_status_endpoint(user_id: str = Depends(get_current_user)):
    from app.utils.scan_status import get_scan_status
    return get_scan_status(user_id)

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

            # Check if user already exists by email first
            invite_email = invite.email or f"{req.invite_code.lower()}@stockbros.app"
            existing = s.execute(text("""
                SELECT id, display_name, email FROM users
                WHERE email = :email OR invited_by = :invited_by
                ORDER BY created_at LIMIT 1
            """), {"email": invite_email, "invited_by": invite.invited_by}).fetchone()

            is_new_user = existing is None

            if existing:
                # User already exists — just log them in
                user_id      = str(existing.id)
                display_name = existing.display_name
                email        = existing.email
            else:
                # New user — create account
                display_name = req.display_name or f"Trader_{req.invite_code[:6]}"
                row = s.execute(text("""
                    INSERT INTO users (display_name, email, invited_by, is_active)
                    VALUES (:name, :email, :invited_by, TRUE)
                    RETURNING id, display_name
                """), {"name": display_name, "email": invite_email,
                       "invited_by": invite.invited_by}).fetchone()
                user_id = str(row.id)
                email   = invite_email

                # Default profile
                s.execute(text("""
                    INSERT INTO user_profiles (user_id, risk_tolerance)
                    VALUES (:uid, 'moderate')
                    ON CONFLICT DO NOTHING
                """), {"uid": user_id})

            # Mark invite accepted
            s.execute(text("""
                UPDATE invites SET status='accepted', accepted_at=now()
                WHERE id = :id
            """), {"id": invite.id})

        # Mint the user's MCP (Claude Desktop) key once, at account creation.
        # Shown in plaintext exactly this one time - only its SHA-256 hash is
        # ever stored (user_api_keys), same as every other key on this
        # platform - so it can't be recovered later, only rotated.
        mcp_key = None
        if is_new_user:
            from app.utils.api_keys import generate_api_key
            from app.db.queries.user_api_keys import create_api_key

            plaintext, key_hash = generate_api_key()
            create_api_key(user_id, key_hash, label="stockbros-signup")
            mcp_key = plaintext

        token = create_token(user_id, email or "")
        return LoginResponse(
            token        = token,
            user_id      = user_id,
            display_name = display_name,
            email        = email,
            mcp_api_key  = mcp_key,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.on_event("startup")
async def startup_event():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        import pytz

        et = pytz.timezone("America/New_York")
        scheduler = BackgroundScheduler(timezone=et)

        def _run_velocity_snapshot():
            try:
                from sqlalchemy import text
                from app.db.session import get_session
                from app.signals.velocity_tracker import save_daily_signals
                with get_session() as s:
                    users = s.execute(text("SELECT id FROM users WHERE is_active=TRUE")).fetchall()
                for u in users:
                    result = save_daily_signals(str(u.id))
                    print(f"[Scheduler] Velocity snapshot: {result}")
            except Exception as e:
                print(f"[Scheduler] Velocity snapshot failed: {e}")

        def _run_nightly_learning():
            try:
                from sqlalchemy import text
                from app.db.session import get_session
                from app.learning.nightly_loop import run_nightly_loop
                from app.recommendations.mark_to_market import mark_all_active_recommendations
                from app.broker.factory import get_broker
                with get_session() as s:
                    users = s.execute(text("SELECT id FROM users WHERE is_active=TRUE")).fetchall()
                for u in users:
                    uid = str(u.id)

                    # Mark every open recommendation to market FIRST — the
                    # learning loop needs today's real P&L, not whatever was
                    # last computed (previously only happened lazily when
                    # the History tab was opened, so nightly learning could
                    # run against stale or entirely-unmarked data on a day
                    # nobody opened the app).
                    try:
                        mark_result = mark_all_active_recommendations(uid, days_back=90)
                        print(f"[Scheduler] Marked {mark_result.get('marked',0)}/"
                              f"{mark_result.get('total',0)} recs for {uid[:8]}")
                    except Exception as e:
                        print(f"[Scheduler] Mark-to-market failed for {uid[:8]}: {e}")

                    try:
                        positions = get_broker(uid).get_positions() or []
                    except Exception:
                        positions = []
                    result = run_nightly_loop(uid, positions)
                    print(f"[Scheduler] Nightly learning: {result.get('ran')} for {uid[:8]}")
            except Exception as e:
                print(f"[Scheduler] Nightly learning failed: {e}")

        scheduler.add_job(
            _run_velocity_snapshot,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=15, timezone=et),
            id="velocity_snapshot", replace_existing=True,
        )
        scheduler.add_job(
            _run_nightly_learning,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone=et),
            id="nightly_learning", replace_existing=True,
        )
        scheduler.start()
        print("[Scheduler] ✅ velocity@4:15PM ET | learning@4:30PM ET (weekdays)")
    except Exception as e:
        print(f"[Startup] Scheduler failed: {e}")

    # Auto-resume position monitoring for anyone with an active
    # tracked position. Without this, any server restart (which
    # happens on every --reload trigger in dev, or any crash/redeploy
    # in production) silently stops the 15-min polling with no
    # indication anything broke.
    try:
        from sqlalchemy import text as _mtext
        from app.db.session import get_session as _mgs
        from app.monitor.position_monitor import get_monitor
        with _mgs() as _ms:
            _active_users = _ms.execute(_mtext(
                "SELECT DISTINCT user_id FROM tracked_positions WHERE is_active=TRUE"
            )).fetchall()
        for _u in _active_users:
            _uid = str(_u.user_id)
            _result = get_monitor(_uid).start()
            print(f"[Startup] Resumed monitoring for {_uid[:8]}: {_result.get('status')}")
        if not _active_users:
            print("[Startup] No active tracked positions — monitor not auto-started")
    except Exception as e:
        print(f"[Startup] Monitor auto-resume failed: {e}")





@app.get("/api/auth/me", tags=["Auth"])
async def get_me(user_id: str = Depends(get_current_user)):
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        with get_session() as s:
            row = s.execute(text(
                "SELECT display_name, email, is_admin, created_at FROM users WHERE id=:uid"
            ), {"uid": user_id}).fetchone()
        return {
            "user_id": user_id,
            "display_name": row.display_name if row else "Trader",
            "is_admin": bool(row.is_admin) if row else False,
        }
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


@app.get("/api/market/quote", tags=["Market"])
async def get_quote_detail(ticker: str, user_id: str = Depends(get_current_user)):
    try:
        from app.options_flow.unusual_whales import get_stock_state
        s = get_stock_state(ticker.upper())
        return s or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/market/ticker-signal", tags=["Market"])
async def get_ticker_signal_detail(ticker: str, user_id: str = Depends(get_current_user)):
    try:
        from app.options_flow.unusual_whales import (
            get_flow_alerts, get_dark_pool_ticker, get_iv_rank
        )
        ticker = ticker.upper()
        flow   = get_flow_alerts(ticker=ticker, limit=20) or []
        dp     = get_dark_pool_ticker(ticker, limit=20)   or []
        iv     = get_iv_rank(ticker)

        bull = sum(1 for a in flow if a.get("sentiment") in ("BULLISH","CALL"))
        bear = sum(1 for a in flow if a.get("sentiment") in ("BEARISH","PUT"))
        tot  = bull + bear
        dp_buy  = sum(1 for d in dp if d.get("side") in ("BUY","A"))
        dp_sell = sum(1 for d in dp if d.get("side") in ("SELL","B"))
        dp_tot  = dp_buy + dp_sell

        flow_score = round((bull-bear)/tot*100, 1) if tot else 0
        dp_score   = round((dp_buy-dp_sell)/dp_tot*100, 1) if dp_tot else 0
        direction  = "BULLISH" if (flow_score+dp_score) > 10 else "BEARISH" if (flow_score+dp_score) < -10 else "NEUTRAL"

        return {
            "ticker":      ticker,
            "direction":   direction,
            "flow_score":  flow_score,
            "dp_score":    dp_score,
            "iv_rank":     iv.get("iv_rank") if iv else None,
            "sweeps":      sum(1 for a in flow if a.get("is_sweep")),
            "alert_count": tot,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/market/news", tags=["Market"])
async def get_ticker_news(ticker: str, user_id: str = Depends(get_current_user)):
    try:
        from app.options_flow.unusual_whales import get_news_headlines
        news = get_news_headlines(ticker=ticker.upper(), limit=10) or []
        return [
            {
                "headline": n.get("headline") or n.get("title") or "",
                "source":   n.get("source", ""),
                "created_at": n.get("created_at", ""),
            }
            for n in news
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        # Get positions + balances (cache or live)
        positions, balances, source = [], {}, "empty"

        if not live:
            cache = get_cached_portfolio(user_id)
            if cache and not cache.get("is_stale"):
                positions = cache["positions"]
                balances  = cache.get("balances") or {}
                source    = "cache"

        if not positions:
            from app.broker.factory import get_broker
            broker = get_broker(user_id)

            # Webull rate limit protection — min 30s between live fetches per user
            import time
            from sqlalchemy import text
            from app.db.session import get_session
            now = time.time()
            _last_fetch = getattr(get_portfolio, f"_last_{user_id}", 0)
            if now - _last_fetch < 30 and not live:
                # Too soon — use cache
                _cache = get_cached_portfolio(user_id)
                if _cache:
                    positions = _cache.get("positions") or []
                    balances  = _cache.get("balances")  or {}
                    source    = "cache_cooldown"
            if not positions:
                try:
                    positions = broker.get_positions() or []
                    balances  = broker.get_balances()   or {}
                    source    = "live"
                    setattr(get_portfolio, f"_last_{user_id}", now)
                except Exception as _we:
                    if "429" in str(_we) or "TOO_MANY" in str(_we):
                        print(f"[Portfolio] Webull 429 — using cache")
                        _cache = get_cached_portfolio(user_id)
                        if _cache:
                            positions = _cache.get("positions") or []
                            balances  = _cache.get("balances")  or {}
                            source    = "cache_429"
                        else:
                            raise
                    else:
                        raise

        # Extract correct values from Webull nested structure
        acct      = (balances.get("account_currency_assets") or [{}])[0]
        net_liq   = float(acct.get("net_liquidation_value") or balances.get("total_market_value") or 0)
        cash      = float(balances.get("total_cash_balance") or acct.get("cash_balance") or 0)
        pos_value = float(balances.get("total_market_value") or acct.get("positions_market_value") or 0)

        pnl  = get_portfolio_pnl_summary(positions, balances) if positions else {}
        bets = get_active_bets(positions, user_id=user_id) if positions else []

        pnl["net_liq"]         = round(net_liq, 2)
        pnl["cash"]            = round(cash, 2)
        pnl["positions_value"] = round(pos_value, 2)
        pnl["buying_power"]    = round(cash, 2)

        # Merge type + option fields from pnl.positions into bets
        pnl_pos = pnl.get("positions", [])
        type_map = {}
        for p in pnl_pos:
            key = (p.get("symbol"), round(float(p.get("qty",0)),0))
            type_map[key] = {
                "type":       p.get("type", "STOCK"),
                "unit_cost":  p.get("unit_cost"),
                "last_price": p.get("last_price"),
            }
        for bet in bets:
            sym  = bet.get("symbol","")
            qty  = round(float(bet.get("qty",0)),0)
            info = type_map.get((sym, qty), {})
            if "type" not in bet or not bet.get("type"):
                bet["type"] = info.get("type","STOCK")
            if not bet.get("unit_cost") and info.get("unit_cost"):
                bet["unit_cost"]  = info["unit_cost"]
            if not bet.get("last_price") and info.get("last_price"):
                bet["last_price"] = info["last_price"]

        return {
            "positions":     pnl_pos,  # typed positions for frontend
            "balances":      balances,
            "pnl":           pnl,
            "bets":          bets,
            "source":        source,
        }
    except BrokerNotConnectedError:
        raise  # handled globally - see broker_not_connected_handler
    except Exception as e:
        import traceback
        print(f"[Portfolio API Error] {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/portfolio/active-bets", tags=["Portfolio"])
async def get_active_bets_api(user_id: str = Depends(get_current_user)):
    try:
        from app.broker.factory import get_broker
        from app.broker.active_bets import get_active_bets
        broker    = get_broker(user_id)
        positions = broker.get_positions()
        return get_active_bets(positions, user_id=user_id)
    except BrokerNotConnectedError:
        raise  # handled globally - see broker_not_connected_handler
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Recommendations
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/recommendations/daily", tags=["Recommendations"])
async def get_daily_recs(
    force_refresh: bool = False,
    budget:    float = 2000.0,
    scan_type: str   = "options",
    horizon:   str   = "1m",
    sector:    str | None = None,
    cap_size:  str | None = None,
    catalyst:  str | None = None,
    watchlist_mode: str = "default_plus_mine",
    # Real user inputs — take priority over the horizon-bucket mapping
    # below when provided. None = fall back to a horizon-derived default.
    trading_window_days: int | None = None,
    stop_loss_pct:       float | None = None,
    profit_target_pct:   float | None = None,
    user_id: str = Depends(get_current_user)
):
    try:
        from app.recommendations.daily_engine import get_active_recommendations
        # Always check cache first — instant response
        cached = get_active_recommendations(user_id)
        if cached and not force_refresh:
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
                "message":         "No picks yet today. Click Scan to generate.",
                "needs_scan":      True,
            }
        # User clicked Scan — use smart engine for best results
        try:
            # ── STOCK scan ────────────────────────────────────────────────
            if scan_type == "stocks":
                from app.recommendations.smart_stock_scan import run_smart_stock_scan
                from app.recommendations.horizon_engine import HORIZON_CONFIG
                from starlette.concurrency import run_in_threadpool

                # Same shim style as the options branch below:
                # run_smart_stock_scan (and get_stock_for_horizon beneath it)
                # take real trading_window_days/stop_loss_pct/profit_target_pct
                # inputs now, not just a horizon bucket. Unlike the options
                # engine (which dropped horizon entirely), get_stock_for_horizon
                # still has its own horizon-aware fallback (STOCK_UPSIDE_TARGETS,
                # config stop_pct, momentum lookback) for when trading_window_days
                # is None - only derive a number here when HORIZON_CONFIG
                # actually has a DTE range to derive from (the "3m" bucket);
                # for pure stock horizons (6m/1yr, no dte_min/dte_max) leave it
                # None so that better per-horizon fallback runs downstream
                # instead of a synthetic day count guessed here.
                stock_trading_window_days = trading_window_days
                if stock_trading_window_days is None:
                    cfg = HORIZON_CONFIG.get(horizon, {})
                    if "dte_min" in cfg:
                        stock_trading_window_days = (cfg["dte_min"] + cfg["dte_max"]) // 2
                stock_scan_kwargs = dict(
                    user_id=user_id, horizon=horizon, budget=budget, top_n=5,
                    watchlist_mode=watchlist_mode,
                    trading_window_days=stock_trading_window_days,
                )
                if stop_loss_pct is not None:
                    stock_scan_kwargs["stop_loss_pct"] = stop_loss_pct
                if profit_target_pct is not None:
                    stock_scan_kwargs["profit_target_pct"] = profit_target_pct

                # run_smart_stock_scan is fully synchronous (yfinance calls,
                # ThreadPoolExecutor().result() waits, ~30-40s runtime) — same
                # blocking-event-loop issue fixed for the options branch last
                # session. Without this, /api/scan/status polls queue as
                # genuinely unprocessed for the full scan duration.
                scan_result = await run_in_threadpool(run_smart_stock_scan, **stock_scan_kwargs)
                results = scan_result.get("stocks", [])
                for rec in results:
                    ticker = rec.get("ticker","")
                    if not ticker: continue
                    try:
                        from app.recommendations.daily_engine import _upsert_recommendation
                        _upsert_recommendation(user_id, {
                            "ticker":           ticker,
                            "horizon":          horizon,
                            "direction":        rec.get("direction", "BULLISH"),
                            "conviction_score": rec.get("fundamental_score", 65),
                            "conviction_tier":  "HIGH" if rec.get("fundamental_score",0)>=75 else "MODERATE",
                            "act_now":          True,
                            "position_size_guidance": "standard",
                            "thesis":           rec.get("thesis", ""),
                            "entry_zone_low":   rec.get("entry_price", 0),
                            "entry_zone_high":  rec.get("entry_price", 0),
                            "entry_trigger":    "AT_MARKET",
                            "target_price":     rec.get("target_price", 0),
                            "target_pct":       rec.get("target_pct", 0),
                            "stop_price":       rec.get("stop_price", 0),
                            "stop_pct":         rec.get("stop_pct", -15),
                            "timeframe":        horizon,
                            "invalidation_conditions": rec.get("invalidation_conditions",""),
                            "strategy":         "STOCK",
                            "legs":             [],
                            "key_news":         "NONE",
                            "warnings":         [],
                            "conviction_breakdown": {},
                            "signal_data":      {"rec_type": "stock"},
                        })
                    except Exception as e:
                        print(f"[StockScan] Store failed {ticker}: {e}")
                return {
                    "recommendations": [],
                    "stocks":          results,
                    "market_view":     f"Top {len(results)} stock picks for {horizon} horizon",
                    "source":          "stock_scan",
                    "count":           len(results),
                }

            # ── OPTIONS scan (default) ────────────────────────────────────────
            from app.recommendations.rescan_engine import rescan_with_validation
            from app.recommendations.horizon_engine import HORIZON_CONFIG
            from app.scanner.quick_scan import quick_scan
            from app.scanner.universe import get_scan_universe

            from starlette.concurrency import run_in_threadpool

            if sector and cap_size:
                picks = None
            else:
                picks = await run_in_threadpool(
                    quick_scan,
                    get_scan_universe(user_id=user_id, watchlist_mode=watchlist_mode),
                    user_id=user_id, top_n=15
                )

            # rescan_with_validation takes real trading_window_days/stop_loss_pct/
            # profit_target_pct inputs, not a horizon bucket string — this endpoint
            # still accepts `horizon` for existing callers, mapped to a window via
            # the same DTE ranges horizon_engine.py already defines, so there's
            # only one copy of "what does '1m' mean in days" in the codebase.
            if trading_window_days is None:
                cfg = HORIZON_CONFIG.get(horizon, HORIZON_CONFIG["1m"])
                trading_window_days = (cfg["dte_min"] + cfg["dte_max"]) // 2
            rescan_kwargs = dict(
                user_id=user_id, budget=budget,
                pre_scanned=picks,
                sector=sector, cap_size=cap_size, catalyst=catalyst,
                trading_window_days=trading_window_days,
            )
            if stop_loss_pct is not None:
                rescan_kwargs["stop_loss_pct"] = stop_loss_pct
            if profit_target_pct is not None:
                rescan_kwargs["profit_target_pct"] = profit_target_pct

            # rescan_with_validation is fully synchronous (blocking HTTP calls
            # to Ollama, yfinance, ThreadPoolExecutor waits, sync DB sessions)
            # and takes ~60-80s. Run it off the event loop so /api/scan/status
            # polls (and every other request) do not queue up "pending" behind
            # it for the entire duration.
            result = await run_in_threadpool(rescan_with_validation, **rescan_kwargs)
            recs = result.get("picks", [])
            return {
                "recommendations": recs,
                "stocks":          [],
                "market_view":     result.get("market_view",""),
                "source":          result.get("source","rescan"),
                "count":           len(recs),
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Scan failed: {e}")
    except HTTPException:
        raise
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


@app.post("/api/signals/save-daily", tags=["Signals"])
async def trigger_daily_snapshot(user_id: str = Depends(get_current_user)):
    """Manually trigger daily signal snapshot (normally runs at 4:15 PM ET)."""
    try:
        from app.signals.velocity_tracker import save_daily_signals
        result = save_daily_signals(user_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/recommendations/history-grouped", tags=["Recommendations"])
async def get_history_grouped(
    days_back: int = 30,
    force_remark: bool = False,
    all_users: bool = False,
    user_id: str = Depends(get_current_user)
):
    """
    Recommendation history grouped by date with mark-to-market P&L.
    Lazy-refresh: re-marks if existing marks are >15 min stale.

    all_users=True (admin only, 403 otherwise): every user's
    recommendations for the date range instead of just the caller's own,
    each pick tagged with user_id/display_name, plus a by_user summary
    (net_pnl/win_rate per customer) alongside the existing by_date one.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        from app.recommendations.mark_to_market import mark_all_active_recommendations
        from datetime import datetime, timezone

        if all_users:
            _require_admin(user_id)

        scope_filter = "" if all_users else "AND dr.user_id = :uid"

        with get_session() as s:
            staleness = s.execute(text(f"""
                SELECT MIN(last_marked_at) as oldest_mark,
                       COUNT(*) FILTER (WHERE last_marked_at IS NULL) as unmarked
                FROM daily_recommendations dr
                WHERE date >= CURRENT_DATE - :days
                  AND status != 'INVALIDATED' {scope_filter}
            """), {"uid": user_id, "days": days_back}).fetchone()

        needs_remark = force_remark
        if staleness:
            if staleness.unmarked and staleness.unmarked > 0:
                needs_remark = True
            elif staleness.oldest_mark:
                age_min = (datetime.now(timezone.utc) - staleness.oldest_mark).total_seconds() / 60
                if age_min > 15:
                    needs_remark = True

        if needs_remark:
            if all_users:
                # Mirrors the nightly scheduled job's own per-user loop
                # (main.py's _run_nightly_learning) - mark_all_active_
                # recommendations only ever marks its one given user_id.
                with get_session() as s:
                    active_users = s.execute(text(
                        "SELECT id FROM users WHERE is_active = TRUE"
                    )).fetchall()
                for u in active_users:
                    try:
                        mark_all_active_recommendations(str(u.id), days_back)
                    except Exception as e:
                        print(f"[HistoryGrouped] Remark failed for {str(u.id)[:8]}: {e}")
            else:
                mark_all_active_recommendations(user_id, days_back)

        with get_session() as s:
            rows = s.execute(text(f"""
                SELECT dr.id, dr.user_id, u.display_name,
                       dr.ticker, dr.direction, dr.strategy, dr.horizon, dr.expiry,
                       dr.conviction_score, dr.conviction_tier, dr.thesis, dr.legs,
                       dr.entry_debit, dr.entry_zone_low, dr.entry_zone_high,
                       dr.current_value, dr.current_pnl_dollars, dr.current_pnl_pct,
                       dr.mark_type, dr.last_marked_at, dr.status, dr.date, dr.created_at
                FROM daily_recommendations dr
                JOIN users u ON u.id = dr.user_id
                WHERE dr.date >= CURRENT_DATE - :days {scope_filter}
                ORDER BY dr.date DESC, dr.conviction_score DESC
            """), {"uid": user_id, "days": days_back}).fetchall()

        grouped: dict = {}
        by_user_agg: dict = {}
        for r in rows:
            pnl_dollars = float(r.current_pnl_dollars) if r.current_pnl_dollars is not None else None
            pick = {
                "id":              str(r.id),
                "ticker":          r.ticker,
                "direction":       r.direction,
                "strategy":        r.strategy,
                "horizon":         r.horizon,
                "expiry":          str(r.expiry) if r.expiry else None,
                "conviction_score": r.conviction_score,
                "conviction_tier": r.conviction_tier,
                "thesis":          r.thesis,
                "legs":            r.legs or [],
                "entry_value":     float(r.entry_debit or r.entry_zone_low or 0),
                "current_value":   float(r.current_value) if r.current_value is not None else None,
                "pnl_dollars":     pnl_dollars,
                "pnl_pct":         float(r.current_pnl_pct) if r.current_pnl_pct is not None else None,
                "mark_type":       r.mark_type,
                "last_marked_at":  str(r.last_marked_at) if r.last_marked_at else None,
                "status":          r.status,
            }
            if all_users:
                # Not needed for the normal per-user view - implicitly "you".
                pick["user_id"]      = str(r.user_id)
                pick["display_name"] = r.display_name or "Trader"

            d = str(r.date)
            grouped.setdefault(d, []).append(pick)

            if all_users:
                uid = str(r.user_id)
                agg = by_user_agg.setdefault(uid, {
                    "user_id": uid, "display_name": r.display_name or "Trader",
                    "net_pnl": 0.0, "total_picks": 0, "marked_picks": 0,
                    "winners": 0, "losers": 0,
                })
                agg["total_picks"] += 1
                if pnl_dollars is not None:
                    agg["marked_picks"] += 1
                    agg["net_pnl"] += pnl_dollars
                    if pnl_dollars > 0: agg["winners"] += 1
                    elif pnl_dollars < 0: agg["losers"] += 1

        result = []
        for date, picks in sorted(grouped.items(), reverse=True):
            marked = [p for p in picks if p["pnl_dollars"] is not None]
            net_pnl = sum(p["pnl_dollars"] for p in marked)
            winners = sum(1 for p in marked if p["pnl_dollars"] > 0)
            losers  = sum(1 for p in marked if p["pnl_dollars"] < 0)
            result.append({
                "date":          date,
                "picks":         picks,
                "total_picks":   len(picks),
                "marked_picks":  len(marked),
                "net_pnl":       round(net_pnl, 2),
                "winners":       winners,
                "losers":        losers,
                "win_rate":      round(winners/len(marked)*100, 1) if marked else None,
            })

        response = {"history": result}
        if all_users:
            by_user = []
            for agg in by_user_agg.values():
                agg["net_pnl"]  = round(agg["net_pnl"], 2)
                agg["win_rate"] = round(agg["winners"]/agg["marked_picks"]*100, 1) if agg["marked_picks"] else None
                by_user.append(agg)
            by_user.sort(key=lambda x: x["net_pnl"], reverse=True)
            response["by_user"] = by_user
        return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/recommendations/open-positions", tags=["Recommendations"])
async def get_open_positions(user_id: str = Depends(get_current_user)):
    """
    Confirmed-filled positions, sourced from daily_recommendations - this
    is every user's equivalent of "portfolio" (broker connection is
    admin-only; see ARCHITECTURE.md's "Broker Connection Is Optional").
    Applies to the admin too - confirming a fill here is separate from
    their real Webull account.

    Note: tracked_positions also records a fill (used by
    position_monitor.py for alerting) but has no thesis/legs/mark-to-
    market P&L, so this reads from daily_recommendations instead - two
    tables recording overlapping fill facts is the same shape of issue
    as the retired strategy_recommendations duplication, worth a look
    eventually, not fixed here.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        from app.recommendations.mark_to_market import mark_all_active_recommendations
        from datetime import datetime, timezone

        # Same lazy-refresh staleness pattern as history-grouped, scoped
        # to open positions instead of a date range.
        with get_session() as s:
            staleness = s.execute(text("""
                SELECT MIN(last_marked_at) as oldest_mark,
                       COUNT(*) FILTER (WHERE last_marked_at IS NULL) as unmarked
                FROM daily_recommendations
                WHERE user_id = :uid AND user_executed = TRUE
                  AND status = 'ACTIVE' AND closed_at IS NULL
            """), {"uid": user_id}).fetchone()

        needs_remark = False
        if staleness:
            if staleness.unmarked and staleness.unmarked > 0:
                needs_remark = True
            elif staleness.oldest_mark:
                age_min = (datetime.now(timezone.utc) - staleness.oldest_mark).total_seconds() / 60
                if age_min > 15:
                    needs_remark = True

        if needs_remark:
            mark_all_active_recommendations(user_id)

        with get_session() as s:
            rows = s.execute(text("""
                SELECT id, ticker, direction, strategy, legs, expiry, dte,
                       actual_entry_price, actual_qty, executed_at,
                       target_pct, stop_pct, thesis,
                       current_value, current_pnl_dollars, current_pnl_pct, mark_type
                FROM daily_recommendations
                WHERE user_id = :uid AND user_executed = TRUE
                  AND status = 'ACTIVE' AND closed_at IS NULL
                ORDER BY executed_at DESC
            """), {"uid": user_id}).fetchall()

        positions = [{
            "id":                  str(r.id),
            "ticker":              r.ticker,
            "direction":           r.direction,
            "strategy":            r.strategy,
            "legs":                r.legs or [],
            "expiry":              str(r.expiry) if r.expiry else None,
            "dte":                 r.dte,
            "actual_entry_price":  float(r.actual_entry_price) if r.actual_entry_price is not None else None,
            "actual_qty":          r.actual_qty,
            "executed_at":         str(r.executed_at) if r.executed_at else None,
            "target_pct":          float(r.target_pct) if r.target_pct is not None else None,
            "stop_pct":            float(r.stop_pct) if r.stop_pct is not None else None,
            "thesis":              r.thesis,
            "current_value":       float(r.current_value) if r.current_value is not None else None,
            "current_pnl_dollars": float(r.current_pnl_dollars) if r.current_pnl_dollars is not None else None,
            "current_pnl_pct":     float(r.current_pnl_pct) if r.current_pnl_pct is not None else None,
            "mark_type":           r.mark_type,
        } for r in rows]

        return {"positions": positions, "count": len(positions)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/recommendations/backtest-stats", tags=["Recommendations"])
async def get_backtest_stats_endpoint(
    days_back: int = 90,
    user_id: str = Depends(get_current_user)
):
    try:
        from app.recommendations.mark_to_market import calculate_backtest_stats
        return calculate_backtest_stats(user_id, days_back)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/recommendations/stocks", tags=["Recommendations"])
async def get_stock_recs(
    budget: float = 5000.0,
    user_id: str = Depends(get_current_user)
):
    try:
        import yfinance as yf
        from app.recommendations.horizon_engine import get_stock_for_horizon
        candidates = ["NVDA", "AAPL", "MSFT"]
        results = []
        for ticker in candidates:
            for horizon in ["3m", "6m", "1yr"]:
                try:
                    price = yf.Ticker(ticker).fast_info.last_price or 0
                    if not price: continue
                    rec = get_stock_for_horizon(ticker, horizon, budget, current_price=price)
                    if rec and not rec.get("filtered"):
                        results.append(rec)
                        break
                except Exception:
                    continue
        return {"stocks": results}
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
    # Returns the admin's shared default list and this user's own
    # additions SEPARATELY, using the same helpers get_scan_universe()
    # relies on. For the admin, these are literally the same rows on
    # both sides — that's expected, not a bug, since the admin's own
    # watchlist IS the shared default.
    from app.scanner.universe import _get_admin_watchlist, _get_user_watchlist
    return {
        "default": sorted(_get_admin_watchlist()),
        "mine":     sorted(_get_user_watchlist(user_id)),
    }


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
    except BrokerNotConnectedError:
        raise  # handled globally - see broker_not_connected_handler
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

@app.get("/api/portfolio/check-fills", tags=["Portfolio"])
async def check_fills(user_id: str = Depends(get_current_user)):
    """
    Auto-detect if user filled a recommendation.
    Compare current positions vs today's active recommendations.
    Called every 30s after scan for 30 min.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        from app.broker.factory import get_broker
        from datetime import date

        broker    = get_broker(user_id)
        try:
            positions = broker.get_positions() or []
        except Exception as pos_err:
            if "429" in str(pos_err) or "TOO_MANY" in str(pos_err):
                # Rate limited — fall back to cached portfolio
                print(f"[Portfolio] Webull 429 — using cached portfolio")
                from sqlalchemy import text
                from app.db.session import get_session
                with get_session() as s:
                    cached = s.execute(text(
                        "SELECT positions, balances FROM portfolio_cache WHERE user_id=:uid"
                    ), {"uid": user_id}).fetchone()
                if cached:
                    return {
                        "positions": cached.positions or [],
                        "balances":  cached.balances or {},
                        "pnl":       {},
                        "bets":      [],
                        "source":    "cache_429",
                    }
            raise
        pos_symbols = {p.get("symbol","") for p in positions}

        with get_session() as s:
            recs = s.execute(text("""
                SELECT ticker, direction, strategy, expiry, conviction_score
                FROM daily_recommendations
                WHERE user_id=:uid AND date=CURRENT_DATE AND status='ACTIVE'
            """), {"uid": user_id}).fetchall()

        matches = []
        for rec in recs:
            ticker = rec.ticker
            if ticker in pos_symbols:
                # Check if already tracked
                with get_session() as s:
                    tracked = s.execute(text("""
                        SELECT id FROM tracked_positions
                        WHERE user_id=:uid AND ticker=:t
                        AND created_at > now() - interval '1 day'
                    """), {"uid": user_id, "t": ticker}).fetchone()

                if not tracked:
                    pos = next((p for p in positions if p.get("symbol")==ticker), {})
                    # Auto-confirm with real price (not 0) to match user entry
                    entry_price = float(pos.get("unit_cost") or pos.get("last_price") or 0)
                    qty         = int(pos.get("qty") or 1)
                    try:
                        from app.learning.prediction_tracker import confirm_execution
                        confirm_execution(user_id, ticker, entry_price, qty,
                                         recommendation_id=None)
                    except Exception:
                        pass  # already tracked
                    matches.append({
                        "ticker":        ticker,
                        "direction":     rec.direction,
                        "strategy":      rec.strategy,
                        "expiry":        rec.expiry,
                        "qty":           qty,
                        "price":         entry_price,
                        "auto_detected": True,
                    })

        return {
            "new_fills": matches,
            "positions_count": len(positions),
            "recs_count": len(recs),
        }
    except BrokerNotConnectedError:
        raise  # handled globally - see broker_not_connected_handler
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
