"""
SEC EDGAR Form 4 Insider Activity Tracker.

Free API — no key required.
Purchases = bullish signal (CEO buying = very strong)
Sales = bearish signal (large sales by insiders = distribution)

API endpoint: https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom
Form 4 filings within 2 business days of transaction.
"""
import requests
import time
from datetime import datetime, timedelta
from functools import lru_cache

HEADERS = {
    "User-Agent": "StockBros trading-platform@example.com",
    "Accept": "application/json",
}

# C-suite roles that carry extra weight
CSUITE = {"chief executive", "ceo", "president", "chief financial", "cfo",
          "chief operating", "coo", "chairman", "director"}


def get_insider_activity(ticker: str, days: int = 5) -> dict:
    """
    Fetch recent Form 4 filings for a ticker.
    Returns summary: has_buy, has_sell, total_value, key_transactions
    """
    try:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{ticker}%22&dateRange=custom"
            f"&startdt={since}&forms=4"
        )
        r = requests.get(url, headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return _empty()

        data = r.json()
        hits = data.get("hits", {}).get("hits", [])

        buys, sells = [], []
        for hit in hits[:20]:
            src = hit.get("_source", {})
            # Verify it's for our ticker
            tickers_in_filing = src.get("period_of_report", "")
            entity = src.get("entity_name", "").upper()
            if ticker.upper() not in src.get("file_num","") and ticker.upper() not in str(src):
                continue

            # Get transaction details from filing
            filing_url = src.get("file_date","")
            accession  = src.get("accession_no","").replace("-","")
            if not accession:
                continue

            transactions = _parse_form4(accession)
            for tx in transactions:
                if tx["transaction_code"] in ("P",):  # P = Purchase
                    buys.append({
                        "date":   tx.get("date",""),
                        "shares": tx.get("shares",0),
                        "price":  tx.get("price",0),
                        "value":  tx.get("value",0),
                        "role":   src.get("entity_name",""),
                        "is_csuite": _is_csuite(src.get("entity_name","")),
                    })
                elif tx["transaction_code"] in ("S",):  # S = Sale
                    sells.append({
                        "date":   tx.get("date",""),
                        "shares": tx.get("shares",0),
                        "price":  tx.get("price",0),
                        "value":  tx.get("value",0),
                        "role":   src.get("entity_name",""),
                        "is_csuite": _is_csuite(src.get("entity_name","")),
                    })

        total_buy_value  = sum(b["value"] for b in buys)
        total_sell_value = sum(s["value"] for s in sells)

        return {
            "has_buy":         len(buys) > 0,
            "has_sell":        len(sells) > 0,
            "buy_count":       len(buys),
            "sell_count":      len(sells),
            "total_value":     total_buy_value + total_sell_value,
            "buy_value":       total_buy_value,
            "sell_value":      total_sell_value,
            "csuite_buy":      any(b["is_csuite"] for b in buys),
            "csuite_sell":     any(s["is_csuite"] for s in sells),
            "transactions":    buys[:3] + sells[:3],
            "signal":          _signal(buys, sells),
        }

    except Exception as e:
        return _empty()


def _parse_form4(accession_no: str) -> list[dict]:
    """Parse a Form 4 filing XML for transaction details."""
    try:
        cik_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum={accession_no}&type=4&dateb=&owner=include&count=10&search_text="
        # Simpler: use the index file
        url = f"https://www.sec.gov/Archives/edgar/data/{accession_no[:10]}/{accession_no}/{accession_no}-index.json"
        r = requests.get(url, headers=HEADERS, timeout=3)
        if r.status_code != 200:
            return []

        idx = r.json()
        # Find the .xml file
        for item in idx.get("directory", {}).get("item", []):
            if item.get("name","").endswith(".xml") and "form4" in item.get("name","").lower():
                xml_url = f"https://www.sec.gov/Archives/edgar/data/{accession_no[:10]}/{accession_no}/{item['name']}"
                return _parse_xml(xml_url)
        return []
    except Exception:
        return []


def _parse_xml(url: str) -> list[dict]:
    """Parse Form 4 XML for transaction table."""
    try:
        import xml.etree.ElementTree as ET
        r = requests.get(url, headers=HEADERS, timeout=3)
        if r.status_code != 200:
            return []

        root = ET.fromstring(r.content)
        txns = []
        for tx in root.findall(".//nonDerivativeTransaction"):
            code  = (tx.findtext("transactionCoding/transactionCode") or "").strip()
            date  = (tx.findtext("transactionDate/value") or "").strip()
            shares = tx.findtext("transactionAmounts/transactionShares/value") or 0
            price  = tx.findtext("transactionAmounts/transactionPricePerShare/value") or 0
            try:
                shares = float(shares)
                price  = float(price)
                value  = shares * price
            except Exception:
                value = 0
            txns.append({"transaction_code": code, "date": date,
                         "shares": shares, "price": price, "value": value})
        return txns
    except Exception:
        return []


def _is_csuite(role: str) -> bool:
    role_lower = role.lower()
    return any(title in role_lower for title in CSUITE)


def _signal(buys: list, sells: list) -> str:
    """Overall insider signal: BULLISH / BEARISH / NEUTRAL."""
    buy_val  = sum(b["value"] for b in buys)
    sell_val = sum(s["value"] for s in sells)
    csuite_buy  = any(b["is_csuite"] for b in buys)
    csuite_sell = any(s["is_csuite"] for s in sells)

    if csuite_buy and buy_val > 100_000:
        return "STRONG_BULLISH"
    if buy_val > sell_val * 2 and buy_val > 50_000:
        return "BULLISH"
    if csuite_sell and sell_val > 500_000:
        return "BEARISH"
    if sell_val > buy_val * 3 and sell_val > 200_000:
        return "BEARISH"
    return "NEUTRAL"


def _empty() -> dict:
    return {
        "has_buy": False, "has_sell": False,
        "buy_count": 0, "sell_count": 0,
        "total_value": 0, "buy_value": 0, "sell_value": 0,
        "csuite_buy": False, "csuite_sell": False,
        "transactions": [], "signal": "NEUTRAL",
    }


def get_insider_signal_for_llm(ticker: str, days: int = 5) -> str:
    """
    Returns a compact string for LLM context.
    e.g. "CEO bought $2.1M 2 days ago" or "No insider activity"
    """
    data = get_insider_activity(ticker, days)
    if data["signal"] == "STRONG_BULLISH":
        return f"C-suite bought ${data['buy_value']/1e6:.1f}M in last {days}d — STRONG signal"
    if data["signal"] == "BULLISH":
        return f"Insider buying ${data['buy_value']/1e3:.0f}K in last {days}d"
    if data["signal"] == "BEARISH":
        return f"Insider selling ${data['sell_value']/1e6:.1f}M in last {days}d"
    return "No significant insider activity"
