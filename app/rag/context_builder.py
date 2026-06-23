"""
C8 RAG Pipeline — Context Builder (W12).

Enriches LLM prompts with historical and real-time context.
Plugs into strategy engine (get_strategy_recommendation) and
sell signals (evaluate_sell_signals_with_llm).

Data sources (in priority order):
    Price history:   Polygon daily bars (6 months)
    Earnings:        UW earnings history (last 4 actual + next upcoming)
    Macro calendar:  UW economic calendar (next 30 days)
    Ticker news:     Polygon news API (per-ticker, AI sentiment insights)
    Global news:     UW market headlines + CNBC + MarketWatch + Fed RSS
    Sector:          UW sector ETF performance vs SPY

Output: ~600-800 token context string injected into LLM prompt.
Session-cached per ticker to avoid redundant API calls.
"""
import time
from datetime import datetime, timedelta

# Session cache — avoids re-fetching same ticker within same analysis run
_context_cache: dict[str, tuple[float, dict]] = {}  # {ticker: (timestamp, context)}
CACHE_TTL = 3600  # 1 hour


# ─────────────────────────────────────────────────────────────────────────────
# Price History Summary
# ─────────────────────────────────────────────────────────────────────────────

def _build_price_context(ticker: str) -> dict:
    """
    6-month price summary from Polygon daily bars.
    Returns: trend, % from 52w high/low, S/R levels, momentum.
    """
    try:
        from app.market_data.polygon_client import get_bars

        from_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        to_date   = datetime.now().strftime("%Y-%m-%d")
        bars      = get_bars(ticker, 1, "day", from_date, to_date)

        if not bars or len(bars) < 20:
            return {}

        closes  = [float(b.get("c", b.get("close", 0))) for b in bars]
        volumes = [float(b.get("v", b.get("volume", 0))) for b in bars]
        current = closes[-1]

        # Key price levels
        high_52w  = max(closes)
        low_52w   = min(closes)
        pct_from_high = round((current - high_52w) / high_52w * 100, 1)
        pct_from_low  = round((current - low_52w) / low_52w * 100, 1)

        # Performance periods
        ret_30d  = round((current - closes[-30]) / closes[-30] * 100, 1) if len(closes) >= 30 else None
        ret_90d  = round((current - closes[-90]) / closes[-90] * 100, 1) if len(closes) >= 90 else None
        ret_6m   = round((current - closes[0])  / closes[0]  * 100, 1)

        # Moving averages
        ma50  = round(sum(closes[-50:]) / min(50, len(closes)), 2)
        ma200 = round(sum(closes) / len(closes), 2)
        above_ma50  = current > ma50
        above_ma200 = current > ma200

        # Volume trend (avg last 10 vs avg prior 30)
        vol_recent = sum(volumes[-10:]) / 10
        vol_prior  = sum(volumes[-40:-10]) / 30 if len(volumes) >= 40 else vol_recent
        vol_trend  = "increasing" if vol_recent > vol_prior * 1.1 else \
                     "decreasing" if vol_recent < vol_prior * 0.9 else "normal"

        # Key support/resistance (simplified: recent swing lows/highs)
        recent = closes[-30:]
        supports    = sorted(set([round(min(recent[i:i+5]), 0) for i in range(0, 25, 5)]))[:3]
        resistances = sorted(set([round(max(recent[i:i+5]), 0) for i in range(0, 25, 5)]), reverse=True)[:3]

        # Trend determination
        if above_ma50 and above_ma200 and (ret_30d or 0) > 0:
            trend = "UPTREND"
        elif not above_ma50 and not above_ma200 and (ret_30d or 0) < 0:
            trend = "DOWNTREND"
        else:
            trend = "CONSOLIDATING"

        return {
            "current_price": current,
            "trend":         trend,
            "above_ma50":    above_ma50,
            "above_ma200":   above_ma200,
            "ma50":          ma50,
            "ma200":         ma200,
            "ret_30d":       ret_30d,
            "ret_90d":       ret_90d,
            "ret_6m":        ret_6m,
            "pct_from_52w_high": pct_from_high,
            "pct_from_52w_low":  pct_from_low,
            "high_52w":      round(high_52w, 2),
            "low_52w":       round(low_52w, 2),
            "volume_trend":  vol_trend,
            "key_supports":    supports,
            "key_resistances": resistances,
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Earnings History
# ─────────────────────────────────────────────────────────────────────────────

def _build_earnings_context(ticker: str) -> dict:
    """
    Last 4 actual earnings + upcoming earnings from UW.
    Returns: dates, EPS vs estimate, post-earnings price reactions.
    """
    try:
        from app.options_flow.unusual_whales import get_ticker_earnings_history

        history = get_ticker_earnings_history(ticker) or []
        today   = datetime.now().date()

        past     = []
        upcoming = None

        for e in history:
            date_str = e.get("report_date") or ""
            if not date_str:
                continue
            try:
                report_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            except Exception:
                continue

            actual_eps   = e.get("actual_eps")
            estimate_eps = e.get("street_mean_est")
            move_1d      = e.get("post_earnings_move_1d")
            expected_move = e.get("expected_move_perc")

            if report_date < today and actual_eps is not None:
                # Past actual result
                beat = None
                if actual_eps and estimate_eps:
                    try:
                        beat = float(actual_eps) > float(estimate_eps)
                    except Exception:
                        pass
                past.append({
                    "date":          date_str[:10],
                    "actual_eps":    actual_eps,
                    "estimate_eps":  estimate_eps,
                    "beat":          beat,
                    "move_1d_pct":   round(float(move_1d) * 100, 1) if move_1d else None,
                    "move_1w_pct":   round(float(e.get("post_earnings_move_1w") or 0) * 100, 1) or None,
                })
            elif report_date >= today and upcoming is None:
                upcoming = {
                    "date":           date_str[:10],
                    "days_away":      (report_date - today).days,
                    "estimate_eps":   estimate_eps,
                    "expected_move":  expected_move,
                    "report_time":    e.get("report_time"),
                }

        # Most recent 4 past results
        past_sorted = sorted(past, key=lambda x: x["date"], reverse=True)[:4]

        # Avg post-earnings move
        moves = [p["move_1d_pct"] for p in past_sorted if p.get("move_1d_pct") is not None]
        avg_move = round(sum(abs(m) for m in moves) / len(moves), 1) if moves else None

        return {
            "upcoming":        upcoming,
            "recent_results":  past_sorted,
            "avg_move_1d_pct": avg_move,
            "beat_rate":       round(sum(1 for p in past_sorted if p.get("beat")) /
                               max(len(past_sorted), 1) * 100) if past_sorted else None,
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Macro Calendar
# ─────────────────────────────────────────────────────────────────────────────

def _build_macro_context() -> dict:
    """
    Upcoming macro events in next 30 days from UW economic calendar.
    Highlights: FOMC, CPI, NFP, GDP, PPI, PCE (high market impact).
    """
    try:
        from app.options_flow.unusual_whales import get_economic_calendar

        HIGH_IMPACT = {
            "fomc", "federal reserve", "interest rate", "cpi", "consumer price",
            "nfp", "nonfarm", "non-farm", "gdp", "ppi", "producer price",
            "pce", "personal consumption", "jobs", "unemployment",
            "retail sales", "payroll",
        }

        events   = get_economic_calendar() or []
        today    = datetime.now()
        cutoff   = today + timedelta(days=30)
        upcoming = []

        for e in events:
            try:
                event_time = datetime.strptime(e["time"][:19], "%Y-%m-%dT%H:%M:%S")
                if today <= event_time <= cutoff:
                    event_name = e.get("event", "").lower()
                    is_high    = any(k in event_name for k in HIGH_IMPACT)
                    upcoming.append({
                        "date":     event_time.strftime("%Y-%m-%d"),
                        "time_et":  event_time.strftime("%H:%M UTC"),
                        "event":    e.get("event"),
                        "period":   e.get("reported_period"),
                        "prev":     e.get("prev"),
                        "forecast": e.get("forecast"),
                        "impact":   "HIGH" if is_high else "MEDIUM",
                    })
            except Exception:
                pass

        high_impact = [e for e in upcoming if e["impact"] == "HIGH"]
        next_high   = high_impact[0] if high_impact else None

        return {
            "upcoming_events":   upcoming[:10],
            "high_impact_count": len(high_impact),
            "next_high_impact":  next_high,
            "days_to_next_key_event": (
                (datetime.strptime(next_high["date"], "%Y-%m-%d") - today).days
                if next_high else None
            ),
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Ticker-Specific News (Polygon)
# ─────────────────────────────────────────────────────────────────────────────

def _build_ticker_news(ticker: str) -> list[dict]:
    """
    Polygon news for specific ticker — includes AI sentiment insights per ticker.
    """
    try:
        import requests
        from app.utils.config import settings

        r = requests.get(
            "https://api.polygon.io/v2/reference/news",
            params={"apiKey": settings.polygon_api_key, "ticker": ticker, "limit": 5},
            timeout=8,
        )
        if r.status_code != 200:
            return []

        articles = r.json().get("results", [])
        result   = []

        for a in articles:
            # Find ticker-specific insight if available
            insight = next(
                (i for i in a.get("insights", []) if i.get("ticker") == ticker.upper()),
                None
            )
            result.append({
                "title":       a.get("title"),
                "published":   a.get("published_utc", "")[:10],
                "publisher":   a.get("publisher", {}).get("name"),
                "description": (a.get("description") or "")[:200],
                "sentiment":   insight.get("sentiment") if insight else None,
                "reasoning":   (insight.get("sentiment_reasoning") or "")[:150] if insight else None,
                "keywords":    a.get("keywords", [])[:5],
            })

        return result
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Global Market News
# ─────────────────────────────────────────────────────────────────────────────

def _build_global_news() -> list[dict]:
    """
    Global market-moving news from multiple sources:
    - UW market headlines (options market perspective)
    - CNBC RSS (equities/macro)
    - MarketWatch RSS (broad market)
    - Federal Reserve RSS (Fed decisions and statements)
    """
    news = []

    # 1. UW global news (no ticker filter)
    try:
        from app.options_flow.unusual_whales import get_news_headlines
        uw_news = get_news_headlines(ticker=None, limit=8) or []
        for item in uw_news:
            if item.get("is_major"):
                news.append({
                    "source":    item.get("source", "UW"),
                    "headline":  item.get("headline"),
                    "sentiment": item.get("sentiment"),
                    "date":      item.get("created_at", "")[:10],
                    "type":      "market",
                })
    except Exception:
        pass

    # 2. Polygon general market news (no ticker filter = broad market)
    try:
        import requests
        from app.utils.config import settings
        r = requests.get(
            "https://api.polygon.io/v2/reference/news",
            params={"apiKey": settings.polygon_api_key, "limit": 5},
            timeout=8,
        )
        if r.status_code == 200:
            for a in r.json().get("results", []):
                news.append({
                    "source":    a.get("publisher", {}).get("name", "Polygon"),
                    "headline":  a.get("title"),
                    "sentiment": None,
                    "date":      a.get("published_utc", "")[:10],
                    "type":      "market",
                    "keywords":  a.get("keywords", [])[:3],
                })
    except Exception:
        pass

    # 3. Fed RSS (Federal Reserve press releases)
    try:
        import requests, xml.etree.ElementTree as ET
        r = requests.get(
            "https://www.federalreserve.gov/feeds/press_all.xml",
            timeout=5, headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            ns   = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns)[:4]:
                title   = entry.find("atom:title", ns)
                updated = entry.find("atom:updated", ns)
                news.append({
                    "source":    "Federal Reserve",
                    "headline":  title.text if title is not None else "",
                    "sentiment": "neutral",
                    "date":      (updated.text or "")[:10] if updated is not None else "",
                    "type":      "fed",
                })
    except Exception:
        pass

    # 4. CNBC Markets RSS
    try:
        import requests, xml.etree.ElementTree as ET
        r = requests.get(
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            timeout=5, headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            root  = ET.fromstring(r.content)
            items = root.findall(".//item")
            for item in items[:5]:
                title = item.find("title")
                pub   = item.find("pubDate")
                news.append({
                    "source":    "CNBC",
                    "headline":  title.text if title is not None else "",
                    "sentiment": None,
                    "date":      (pub.text or "")[:16] if pub is not None else "",
                    "type":      "market",
                })
    except Exception:
        pass

    # 5. MarketWatch RSS
    try:
        import requests, xml.etree.ElementTree as ET
        r = requests.get(
            "https://feeds.marketwatch.com/marketwatch/topstories/",
            timeout=5, headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            root  = ET.fromstring(r.content)
            items = root.findall(".//item")
            for item in items[:4]:
                title = item.find("title")
                pub   = item.find("pubDate")
                news.append({
                    "source":    "MarketWatch",
                    "headline":  title.text if title is not None else "",
                    "sentiment": None,
                    "date":      (pub.text or "")[:16] if pub is not None else "",
                    "type":      "market",
                })
    except Exception:
        pass

    # Deduplicate by headline similarity, limit total
    seen, result = set(), []
    for item in news:
        headline = (item.get("headline") or "").strip()[:60]
        if headline and headline not in seen:
            seen.add(headline)
            result.append(item)

    return result[:15]


# ─────────────────────────────────────────────────────────────────────────────
# Sector Momentum
# ─────────────────────────────────────────────────────────────────────────────

# Ticker → sector ETF mapping
SECTOR_MAP = {
    # Technology
    "NVDA": "XLK", "AMD": "XLK", "INTC": "XLK", "AVGO": "XLK",
    "MSFT": "XLK", "AAPL": "XLK", "GOOGL": "XLK", "META": "XLK",
    "AMZN": "XLK", "CRM": "XLK", "NOW": "XLK", "PLTR": "XLK",
    "ARM": "XLK", "SNDK": "XLK", "WDC": "XLK", "MU": "XLK",
    "CRWD": "XLK", "IBM": "XLK",
    # Energy
    "CEG": "XLE", "ETN": "XLI", "GEV": "XLI", "VRT": "XLI",
    "XOM": "XLE", "CVX": "XLE",
    # Financials
    "JPM": "XLF", "GS": "XLF",
    # Consumer
    "AMZN": "XLY", "TSLA": "XLY", "NKE": "XLY",
    # Healthcare
    "HIMS": "XLV",
    # Materials/Metals
    "GLD": "GLD", "SLV": "SLV",
    # Defense/Aerospace
    "RTX": "ITA", "LMT": "ITA",
}


def _build_sector_context(ticker: str) -> dict:
    """
    Sector ETF performance vs SPY for the ticker's sector.
    Shows whether the sector is leading or lagging the market.
    """
    try:
        from app.market_data.polygon_client import get_previous_close

        sector_etf = SECTOR_MAP.get(ticker.upper(), "QQQ")

        # Get previous close for sector ETF and SPY
        sector_q = get_previous_close(sector_etf)
        spy_q    = get_previous_close("SPY")

        if not sector_q or not spy_q:
            return {"sector_etf": sector_etf}

        sector_chg = round((sector_q["close"] - sector_q["open"]) / sector_q["open"] * 100, 2)
        spy_chg    = round((spy_q["close"] - spy_q["open"]) / spy_q["open"] * 100, 2)
        rel_str    = round(sector_chg - spy_chg, 2)

        return {
            "sector_etf":          sector_etf,
            "sector_change_pct":   sector_chg,
            "spy_change_pct":      spy_chg,
            "relative_strength":   rel_str,
            "sector_vs_market":    "outperforming" if rel_str > 0.2 else
                                   "underperforming" if rel_str < -0.2 else "in line",
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def _build_vix_context() -> dict:
    """
    VIX trend and zone from yfinance.
    Primary: yfinance ^VIX (actual spot VIX)
    Polygon needs paid plan for I:VIX index.

    Zones:
        < 15   = LOW      → buy options freely, debit spreads ideal
        15-20  = NORMAL   → standard strategy selection
        20-25  = ELEVATED → prefer spreads over naked options
        25-30  = HIGH     → sell premium only, avoid buying
        > 30   = EXTREME  → avoid new positions entirely
    """
    try:
        import yfinance as yf

        hist = yf.Ticker('^VIX').history(period='30d')
        if hist.empty:
            return {"error": "VIX data unavailable"}

        closes  = hist['Close'].tolist()
        current = round(closes[-1], 2)
        prev_5d = round(closes[-5], 2)  if len(closes) >= 5  else None
        prev_30d = round(closes[0], 2)  if len(closes) >= 30 else None

        # Trend over last 5 days
        if prev_5d:
            change_5d = round(current - prev_5d, 2)
            if change_5d > 2:
                trend = "RISING_FAST"
            elif change_5d > 0.5:
                trend = "RISING"
            elif change_5d < -2:
                trend = "FALLING_FAST"
            elif change_5d < -0.5:
                trend = "FALLING"
            else:
                trend = "STABLE"
        else:
            trend    = "UNKNOWN"
            change_5d = None

        # Zone classification
        if current < 15:
            zone        = "LOW"
            implication = "Options cheap — debit spreads and naked options are cost-effective"
            strategy    = "BUY_OPTIONS"
        elif current < 20:
            zone        = "NORMAL"
            implication = "Normal volatility — standard strategy selection applies"
            strategy    = "STANDARD"
        elif current < 25:
            zone        = "ELEVATED"
            implication = "IV elevated — prefer spreads over naked options to reduce cost"
            strategy    = "PREFER_SPREADS"
        elif current < 30:
            zone        = "HIGH"
            implication = "High fear — sell premium (credit spreads), avoid buying options"
            strategy    = "SELL_PREMIUM"
        else:
            zone        = "EXTREME"
            implication = "Extreme fear — avoid new positions, capital preservation mode"
            strategy    = "NO_NEW_POSITIONS"

        # Warning if rising fast
        warning = None
        if trend in ("RISING_FAST",) and current > 20:
            warning = "VIX rising fast above 20 — reduce position size or wait for stabilization"
        elif trend in ("RISING",) and current > 25:
            warning = "VIX rising above 25 — avoid buying options, credit spreads only"

        return {
            "current":      current,
            "prev_5d":      prev_5d,
            "prev_30d":     prev_30d,
            "change_5d":    change_5d,
            "trend":        trend,
            "zone":         zone,
            "implication":  implication,
            "strategy":     strategy,
            "warning":      warning,
            "bars_available": len(closes),
        }

    except Exception as e:
        return {"error": str(e)}


def build_ticker_context(ticker: str, include_global_news: bool = True) -> dict:
    """
    Build full RAG context for any ticker.

    Fetches in parallel where possible:
    - 6-month price history summary (trend, S/R, momentum)
    - Earnings history (last 4 actual + next upcoming + avg reaction)
    - Macro calendar (next 30 days, high-impact events)
    - Ticker-specific news (Polygon, with AI sentiment insights)
    - Global market news (UW + CNBC + MarketWatch + Fed RSS)
    - Sector momentum vs SPY

    Session-cached for 1 hour per ticker.

    Returns dict with all context + formatted_prompt string ready for LLM injection.
    """
    ticker = ticker.upper()

    # Check session cache
    if ticker in _context_cache:
        ts, cached = _context_cache[ticker]
        if time.time() - ts < CACHE_TTL:
            return cached

    print(f"[RAG] Building context for {ticker}...")
    t0 = time.time()

    # Fetch all context (most are fast, global news takes ~2-3s)
    price    = _build_price_context(ticker)
    earnings = _build_earnings_context(ticker)
    macro    = _build_macro_context()
    vix      = _build_vix_context()
    t_news   = _build_ticker_news(ticker)
    sector   = _build_sector_context(ticker)
    g_news   = _build_global_news() if include_global_news else []

    ctx = {
        "ticker":        ticker,
        "price":         price,
        "earnings":      earnings,
        "macro":         macro,
        "vix":           vix,
        "ticker_news":   t_news,
        "global_news":   g_news,
        "sector":        sector,
        "built_at":      datetime.now().isoformat(),
        "build_time_s":  round(time.time() - t0, 1),
    }

    ctx["formatted_prompt"] = _format_for_llm(ctx)

    # Cache it
    _context_cache[ticker] = (time.time(), ctx)
    print(f"[RAG] Context built in {ctx['build_time_s']}s")

    return ctx


def _format_for_llm(ctx: dict) -> str:
    """
    Format context dict into a compact ~700 token prompt string for LLM injection.
    Clear sections, factual, no filler.
    """
    ticker  = ctx["ticker"]
    price   = ctx.get("price", {})
    earn    = ctx.get("earnings", {})
    macro   = ctx.get("macro", {})
    t_news  = ctx.get("ticker_news", [])
    g_news  = ctx.get("global_news", [])
    sector  = ctx.get("sector", {})

    lines = [f"=== MARKET CONTEXT FOR {ticker} ==="]

    # Price
    if price and not price.get("error"):
        lines.append("\n[PRICE & TREND]")
        lines.append("Current: ${} | Trend: {} | MA50: {} | MA200: {}".format(
            price.get("current_price"), price.get("trend"),
            price.get("ma50"), price.get("ma200")))
        lines.append("30d: {:+.1f}% | 90d: {:+.1f}% | 6m: {:+.1f}%".format(
            price.get("ret_30d") or 0,
            price.get("ret_90d") or 0,
            price.get("ret_6m") or 0,
        ))
        lines.append("52w High: ${} ({:+.1f}%) | 52w Low: ${} ({:+.1f}%)".format(
            price.get("high_52w"), price.get("pct_from_52w_high") or 0,
            price.get("low_52w"), price.get("pct_from_52w_low") or 0,
        ))
        if price.get("key_supports"):
            lines.append("Support: {} | Resistance: {}".format(
                price.get("key_supports"), price.get("key_resistances")))
        lines.append("Volume: {}".format(price.get("volume_trend")))

    # Earnings
    if earn and not earn.get("error"):
        lines.append("\n[EARNINGS]")
        upcoming = earn.get("upcoming")
        if upcoming:
            lines.append("NEXT: {} ({} days away) | Est EPS: {} | Expected move: {}%".format(
                upcoming.get("date"), upcoming.get("days_away"),
                upcoming.get("estimate_eps"), upcoming.get("expected_move") or "unknown",
            ))
        else:
            lines.append("No upcoming earnings in 30 days")

        past = earn.get("recent_results", [])
        if past:
            lines.append("Last 4 results (date | beat | 1d move):")
            for r in past:
                beat = "✓ beat" if r.get("beat") else "✗ miss" if r.get("beat") is False else "?"
                move = "{:+.1f}%".format(r["move_1d_pct"]) if r.get("move_1d_pct") else "n/a"
                lines.append("  {} | {} | {}".format(r["date"], beat, move))

        if earn.get("avg_move_1d_pct"):
            lines.append("Avg post-earnings move: ±{:.1f}% | Beat rate: {}%".format(
                earn["avg_move_1d_pct"], earn.get("beat_rate", "?"),
            ))

    # VIX
    vix = ctx.get("vix", {})
    if vix and not vix.get("error"):
        lines.append("\n[VIX — MARKET FEAR]")
        warn = f" ⚠️  {vix['warning']}" if vix.get("warning") else ""
        lines.append("VIX: {} | Zone: {} | Trend: {} (5d change: {:+.1f}){}".format(
            vix.get("current"), vix.get("zone"),
            vix.get("trend"), vix.get("change_5d") or 0, warn
        ))
        lines.append("Implication: {}".format(vix.get("implication")))
        lines.append("Strategy guidance: {}".format(vix.get("strategy")))

    # Sector
    if sector and not sector.get("error"):
        lines.append("\n[SECTOR: {}]".format(sector.get("sector_etf")))
        lines.append("Sector {:+.2f}% vs SPY {:+.2f}% → {} ({:+.2f}% rel)".format(
            sector.get("sector_change_pct", 0),
            sector.get("spy_change_pct", 0),
            sector.get("sector_vs_market", "unknown"),
            sector.get("relative_strength", 0),
        ))

    # Macro calendar
    if macro and not macro.get("error"):
        lines.append("\n[MACRO CALENDAR — NEXT 30 DAYS]")
        high = [e for e in macro.get("upcoming_events", []) if e["impact"] == "HIGH"]
        if high:
            for e in high[:5]:
                lines.append("  {} — {} ({}) | Prev: {} Fcst: {}".format(
                    e["date"], e["event"], e.get("period", ""),
                    e.get("prev", "?"), e.get("forecast", "?"),
                ))
        else:
            lines.append("  No high-impact events in next 30 days")

    # Ticker news
    if t_news:
        lines.append("\n[{} NEWS]".format(ticker))
        for n in t_news[:4]:
            sentiment = " [{}]".format(n["sentiment"].upper()) if n.get("sentiment") else ""
            lines.append("  • {}{}".format(n.get("title", ""), sentiment))
            if n.get("reasoning"):
                lines.append("    → {}".format(n["reasoning"]))

    # Global news (Fed + macro first, then market)
    if g_news:
        lines.append("\n[GLOBAL MARKET NEWS]")
        fed   = [n for n in g_news if n.get("type") == "fed"][:2]
        mkt   = [n for n in g_news if n.get("type") == "market"][:6]
        for n in fed + mkt:
            src  = n.get("source", "")
            head = n.get("headline", "")
            if head:
                lines.append("  [{}] {}".format(src, head[:120]))

    lines.append("\n=== END CONTEXT ===")
    return "\n".join(lines)


def get_context_for_prompt(ticker: str) -> str:
    """
    Convenience function — returns just the formatted prompt string.
    Use this to inject context into any LLM call.
    """
    return build_ticker_context(ticker).get("formatted_prompt", "")


def clear_cache(ticker: str | None = None) -> None:
    """Clear context cache. None = clear all."""
    global _context_cache
    if ticker:
        _context_cache.pop(ticker.upper(), None)
    else:
        _context_cache.clear()