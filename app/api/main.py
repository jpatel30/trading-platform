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

            # Check if user already exists by email first
            invite_email = invite.email or f"{req.invite_code.lower()}@stockbros.app"
            existing = s.execute(text("""
                SELECT id, display_name, email FROM users
                WHERE email = :email OR invited_by = :invited_by
                ORDER BY created_at LIMIT 1
            """), {"email": invite_email, "invited_by": invite.invited_by}).fetchone()

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
            broker    = get_broker(user_id)
            positions = broker.get_positions() or []
            balances  = broker.get_balances() or {}
            source    = "live"

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
    except Exception as e:
        import traceback
        print(f"[Portfolio API Error] {traceback.format_exc()}")
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
    budget:    float = 2000.0,
    scan_type: str   = "options",
    horizon:   str   = "1m",
    sector:    str | None = None,
    cap_size:  str | None = None,
    catalyst:  str | None = None,
    user_id: str = Depends(get_current_user)
):
    try:
        from app.recommendations.daily_engine import (
            run_daily_recommendations, get_active_recommendations
        )
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
                import yfinance as yf
                from app.recommendations.horizon_engine import get_stock_for_horizon
                from app.scanner.quick_scan import quick_scan
                from app.scanner.universe import get_scan_universe

                tickers  = get_scan_universe(user_id=user_id)
                picks    = quick_scan(tickers, user_id=user_id, top_n=20)

                # Filter to liquid stocks only (options not relevant for stock scan)
                stock_picks = [p for p in picks
                               if p.get("price", 0) >= 5
                               and p.get("ticker") not in {"SPY","QQQ","IWM","GLD","SLV"}]

                results = []
                seen    = set()
                for pick in stock_picks[:10]:
                    ticker = pick["ticker"]
                    if ticker in seen:
                        continue
                    seen.add(ticker)
                    try:
                        price = yf.Ticker(ticker).fast_info.last_price or 0
                        if not price:
                            continue
                        rec = get_stock_for_horizon(ticker, horizon, budget, current_price=price)
                        if rec and not rec.get("filtered"):
                            rec["status"] = "NEW"
                            results.append(rec)

                            # Persist to daily_recommendations for History tab
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
                                    "entry_zone_low":   rec.get("entry_price", price),
                                    "entry_zone_high":  rec.get("entry_price", price),
                                    "entry_trigger":    "AT_MARKET",
                                    "target_price":     rec.get("target_price", 0),
                                    "target_pct":       rec.get("target_pct", 0),
                                    "stop_price":       rec.get("stop_price", 0),
                                    "stop_pct":         rec.get("stop_pct", -15),
                                    "timeframe":        horizon,
                                    "invalidation_conditions": rec.get("invalidation_conditions", ""),
                                    "strategy":         "STOCK",
                                    "legs":             [],
                                    "key_news":         "NONE",
                                    "warnings":         [],
                                    "conviction_breakdown": {},
                                    "signal_data":      {"rec_type": "stock"},
                                })
                            except Exception as e:
                                print(f"[StockScan] Store failed for {ticker}: {e}")

                        if len(results) >= 5:
                            break
                    except Exception:
                        continue

                return {
                    "recommendations": [],
                    "stocks":          results,
                    "market_view":     f"Top {len(results)} stock picks for {horizon} horizon",
                    "source":          "stock_scan",
                    "count":           len(results),
                }

            # ── OPTIONS scan (default) ────────────────────────────────────────
            from app.recommendations.rescan_engine import rescan_with_validation
            from app.scanner.quick_scan import quick_scan
            from app.scanner.universe import get_scan_universe

            if sector and cap_size:
                picks = None
            else:
                picks = quick_scan(get_scan_universe(user_id=user_id), user_id=user_id, top_n=15)

            result = rescan_with_validation(
                user_id=user_id, budget=budget,
                pre_scanned=picks,
                sector=sector, cap_size=cap_size, catalyst=catalyst,
            )
            recs = result.get("picks", [])
            return {
                "recommendations": recs,
                "stocks":          [],
                "market_view":     result.get("market_view",""),
                "source":          result.get("source","rescan"),
                "count":           len(recs),
            }
        except Exception as e:
            print(f"[API] Smart engine failed: {e}, falling back")
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


@app.get("/api/recommendations/history-grouped", tags=["Recommendations"])
async def get_history_grouped(
    days_back: int = 30,
    force_remark: bool = False,
    user_id: str = Depends(get_current_user)
):
    """
    Recommendation history grouped by date with mark-to-market P&L.
    Lazy-refresh: re-marks if existing marks are >15 min stale.
    """
    try:
        from sqlalchemy import text
        from app.db.session import get_session
        from app.recommendations.mark_to_market import mark_all_active_recommendations
        from datetime import datetime, timezone

        with get_session() as s:
            staleness = s.execute(text("""
                SELECT MIN(last_marked_at) as oldest_mark,
                       COUNT(*) FILTER (WHERE last_marked_at IS NULL) as unmarked
                FROM daily_recommendations
                WHERE user_id=:uid AND date >= CURRENT_DATE - :days
                  AND status != 'INVALIDATED'
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
            mark_all_active_recommendations(user_id, days_back)

        with get_session() as s:
            rows = s.execute(text("""
                SELECT id, ticker, direction, strategy, horizon, expiry,
                       conviction_score, conviction_tier, thesis, legs,
                       entry_debit, entry_zone_low, entry_zone_high,
                       current_value, current_pnl_dollars, current_pnl_pct,
                       mark_type, last_marked_at, status, date, created_at
                FROM daily_recommendations
                WHERE user_id = :uid AND date >= CURRENT_DATE - :days
                ORDER BY date DESC, conviction_score DESC
            """), {"uid": user_id, "days": days_back}).fetchall()

        grouped: dict = {}
        for r in rows:
            d = str(r.date)
            grouped.setdefault(d, []).append({
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
                "pnl_dollars":     float(r.current_pnl_dollars) if r.current_pnl_dollars is not None else None,
                "pnl_pct":         float(r.current_pnl_pct) if r.current_pnl_pct is not None else None,
                "mark_type":       r.mark_type,
                "last_marked_at":  str(r.last_marked_at) if r.last_marked_at else None,
                "status":          r.status,
            })

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

        return {"history": result}
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
        positions = broker.get_positions() or []
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
