"""
News Agent (MULTIAGENT_MIGRATION.md items 5-6).

Extracted out of app/rag/context_builder.py, not just wrapped - these
two functions (global market news + the economic-calendar/macro-event
wiring) had no ticker-specific dependency on the rest of that module,
so unlike Search Agent's consolidation (item 1, which orchestrates
proven code left in place because 3 different files already depended
on those exact call sites), this one genuinely moves. context_builder.py
now imports both back from here for its own build_ticker_context() use,
same as any other caller - single source of truth, no duplicate copy
left behind.

build_global_news(): UW market headlines + Polygon news + CNBC/
MarketWatch/Federal Reserve RSS, deduplicated by headline.

build_macro_context(): upcoming economic events (next 30 days) from
UW's economic calendar, classified HIGH/MEDIUM impact (FOMC/CPI/NFP/
GDP/PPI/PCE/jobs = HIGH), with days-to-next-key-event.

run_news_agent(): single entry point combining both, per Phase A's
"one entry point per agent" pattern - same shape as
agents/search_agent.py::run_search_agent().
"""
import time
from datetime import datetime, timedelta


def build_global_news() -> list[dict]:
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


def build_macro_context() -> dict:
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


def run_news_agent(trigger: str = "manual") -> dict:
    """
    Single entry point combining global news + macro calendar, per
    MULTIAGENT_MIGRATION.md item 6's "second daily trigger" (6am PT
    pre-open, alongside a new post-close slot - unlike Search Agent,
    News Agent had no existing schedule to pair with, so both slots
    are new). trigger is informational only (identifies which
    scheduled slot called this in logs), same convention as
    agents/search_agent.py::run_search_agent().
    """
    t0 = time.time()
    print(f"[NewsAgent] Starting ({trigger})...")

    news  = build_global_news()
    macro = build_macro_context()

    elapsed = round(time.time() - t0, 1)
    print(f"[NewsAgent] Done ({trigger}) in {elapsed}s — "
          f"{len(news)} headlines, {macro.get('high_impact_count', 0)} high-impact "
          f"events in next 30d")

    return {
        "agent": "news",
        "trigger": trigger,
        "news": news,
        "macro": macro,
        "elapsed": elapsed,
    }
