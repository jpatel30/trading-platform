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

    Two bugs fixed here (found via a live 131-ticker run + direct EDGAR
    API testing where 0/131 tickers ever showed any insider signal,
    including routine sells - not plausible sparsity, a systemic parse
    failure):
      1. dateRange=custom with startdt but no enddt does not actually
         scope results - a "last N days" query was returning hits from
         as far back as 2003. enddt (today) is required for the range
         to apply.
      2. Every hit was then read via src.get("accession_no", "") to
         build the filing's accession number - that field does not
         exist in EDGAR's response at all (the real field is "adsh").
         `if not accession: continue` silently dropped every single
         hit, unconditionally, regardless of #1.
    Also replaced the two-step "fetch an index page, guess which .xml
    file is the real filing" approach in the old _parse_form4 with a
    direct fetch: the search hit's own "_id" field already contains
    the exact primary document filename ("{adsh}:{filename}"), and
    "_source.ciks" already contains the issuer's CIK (last element) -
    both needed to build the one real Archives URL directly, verified
    against a live filing (Apple, Form 4, May 2026).
    """
    try:
        since  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        until  = datetime.now().strftime("%Y-%m-%d")

        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{ticker}%22&dateRange=custom"
            f"&startdt={since}&enddt={until}&forms=4"
        )
        r = requests.get(url, headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return _empty()

        data = r.json()
        hits = data.get("hits", {}).get("hits", [])

        buys, sells = [], []
        for hit in hits[:20]:
            src = hit.get("_source", {})

            adsh = src.get("adsh", "")
            ciks = src.get("ciks", [])
            doc_id = hit.get("_id", "")
            if not adsh or not ciks or ":" not in doc_id:
                continue

            issuer_cik = ciks[-1].lstrip("0") or "0"   # issuer is consistently last
            filename   = doc_id.split(":", 1)[1]
            accession_nodashes = adsh.replace("-", "")

            filing = _parse_form4(issuer_cik, accession_nodashes, filename)

            # Verify it's actually for our ticker using the filing's own
            # issuerTradingSymbol - display_names varies by filing era
            # (older filings show "(TICKER)", current ones only show
            # "(CIK ...)"), so it isn't a reliable filter; the ticker
            # symbol inside the filing itself always is. The search
            # query already does most of the narrowing - this is a
            # correctness check, not the primary filter.
            if filing["issuer_ticker"] and filing["issuer_ticker"].upper() != ticker.upper():
                continue

            role      = filing["owner_name"]
            is_csuite = filing["is_officer"] and _is_csuite(filing["officer_title"])

            for tx in filing["transactions"]:
                if tx["transaction_code"] in ("P",):  # P = Purchase
                    buys.append({
                        "date":   tx.get("date",""),
                        "shares": tx.get("shares",0),
                        "price":  tx.get("price",0),
                        "value":  tx.get("value",0),
                        "role":   role,
                        "is_csuite": is_csuite,
                    })
                elif tx["transaction_code"] in ("S",):  # S = Sale
                    sells.append({
                        "date":   tx.get("date",""),
                        "shares": tx.get("shares",0),
                        "price":  tx.get("price",0),
                        "value":  tx.get("value",0),
                        "role":   role,
                        "is_csuite": is_csuite,
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


_EMPTY_FILING = {
    "issuer_ticker": "", "owner_name": "", "is_officer": False,
    "officer_title": "", "transactions": [],
}


def _parse_form4(issuer_cik: str, accession_no: str, filename: str) -> dict:
    """
    Fetch and parse a Form 4 filing XML.

    issuer_cik/accession_no/filename come straight from the search hit
    (issuer CIK from _source.ciks, accession from _source.adsh, filename
    from _id) - a direct, single fetch of the known real document,
    replacing a previous two-step "fetch an index page, then guess
    which .xml file inside it is the real filing" approach that used
    accession_no[:10] as a stand-in for CIK (it isn't one - that's the
    filing AGENT's CIK, not the issuer's, and doesn't resolve).
    """
    try:
        xml_url = f"https://www.sec.gov/Archives/edgar/data/{issuer_cik}/{accession_no}/{filename}"
        return _parse_xml(xml_url)
    except Exception:
        return dict(_EMPTY_FILING)


def _parse_xml(url: str) -> dict:
    """
    Parse a Form 4 XML: issuer ticker (for verifying the search hit
    actually matches the requested ticker - EDGAR's search-hit metadata
    doesn't reliably carry this, but every filing's own XML does),
    reporting-owner name/officer-title (for C-suite weighting - the
    search hit has no entity_name field at all, despite the old code
    reading one), and the non-derivative transaction table.
    """
    try:
        import xml.etree.ElementTree as ET
        r = requests.get(url, headers=HEADERS, timeout=3)
        if r.status_code != 200:
            return dict(_EMPTY_FILING)

        root = ET.fromstring(r.content)

        issuer_ticker = (root.findtext("issuer/issuerTradingSymbol") or "").strip()
        owner_name    = (root.findtext(
            "reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
        is_officer    = (root.findtext(
            "reportingOwner/reportingOwnerRelationship/isOfficer") or "").strip().lower() == "true"
        # Form 4 carries isOfficer and isDirector as separate flags -
        # officerTitle is only populated when isOfficer is true, but a
        # board member filing purely as a director (isOfficer=false)
        # still counts as C-suite per the CSUITE set below (it includes
        # the bare word "director"/"chairman").
        is_director   = (root.findtext(
            "reportingOwner/reportingOwnerRelationship/isDirector") or "").strip().lower() == "true"
        officer_title = (root.findtext(
            "reportingOwner/reportingOwnerRelationship/officerTitle") or "").strip()
        if is_director and not officer_title:
            officer_title = "director"

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

        return {
            "issuer_ticker": issuer_ticker, "owner_name": owner_name,
            "is_officer": is_officer or is_director, "officer_title": officer_title,
            "transactions": txns,
        }
    except Exception:
        return dict(_EMPTY_FILING)


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
