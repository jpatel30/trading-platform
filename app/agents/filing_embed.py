"""
Filing embed pipeline (MULTIAGENT_MIGRATION.md Search Agent, item 4).

Fetches SEC EDGAR material-event filings (8-K) for watchlist tickers,
chunks the extracted text, embeds via a local Ollama model
(nomic-embed-text), and stores in ChromaDB - the corpus Retrieval
Library's semantic search (item 15) is meant to later query. This is
the first real use of the ChromaDB container (item 17's "actually wire
it up" question), on the write side; Retrieval Library's own query-time
code is a separate, later step.

Transcripts (the other half of item 4) are deliberately NOT built here.
Verified live against UW's real API, not assumed: GET
/api/companies/{ticker}/transcripts/{quarter} returns a consistent 403
{"code":"advanced_tier_required"} regardless of quarter format - this
account does not have that tier. UW's only accessible "filings"
endpoint, /api/institutions/latest_filings, is institutional 13F
metadata only (who filed, CIK, date) - no actual document text to
chunk. SEC EDGAR full-text search (already used for Form 4 in
edgar_insider.py) is the real, free, currently-accessible source used
here instead - expanded from Form 4 (already covered by
edgar_insider.py's own buy/sell signal) to 8-K current reports
(material events: earnings releases, executive changes, M&A,
guidance), a better fit for "the kind of document an analyst would
want to search over" than structured Form 4 transaction data.

Incremental by design: checks ChromaDB for each candidate document's
ID BEFORE fetching/parsing/embedding it - an already-embedded document
is skipped before any network cost, not just before the final write.

CIK-verified by design - found live, the hard way, on the first real
run: EDGAR's full-text search (q="TICKER") matches the ticker STRING
anywhere in a document's text, not filings actually filed by that
company. A search for "NVDA" returned a Canadian Derivatives Clearing
Corporation options-listing exhibit as a "hit" - NVDA appeared once,
as one line in a multi-thousand-row table of unrelated cross-listed
tickers, nothing to do with NVIDIA. edgar_insider.py already guards
against this same failure mode for Form 4 (checks the filing's own
issuerTradingSymbol field), but 8-Ks have no equivalent standardized
field to check post-fetch. Fixed upstream instead: resolve each
ticker's real CIK once via SEC's own company_tickers.json mapping,
then only accept search hits whose ciks field actually contains that
CIK - a precise, structural check, not a text-match heuristic.
"""
import time

COLLECTION_NAME = "sec_filings"
CHUNK_SIZE      = 1500   # chars, not tokens - simple, no tokenizer dependency
CHUNK_OVERLAP   = 200
MIN_TEXT_LEN    = 200    # shorter than this isn't real filing content

HEADERS = {"User-Agent": "StockBros trading-platform@example.com", "Accept": "application/json"}

_cik_cache: dict[str, str] = {}


def _resolve_cik(ticker: str) -> str | None:
    """Ticker -> zero-stripped CIK, via SEC's own official mapping. Cached
    per-process since the mapping file (~10k tickers) never changes within
    a run and is the same for every ticker looked up."""
    import requests

    if not _cik_cache:
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json",
                              headers=HEADERS, timeout=10)
            r.raise_for_status()
            for row in r.json().values():
                _cik_cache[row["ticker"].upper()] = str(row["cik_str"])
        except Exception as e:
            print(f"[FilingEmbed] CIK mapping fetch failed: {e}")
            return None
    return _cik_cache.get(ticker.upper())


def _get_chroma_collection():
    import chromadb
    client = chromadb.HttpClient(host="localhost", port=8000)
    return client.get_or_create_collection(COLLECTION_NAME)


def _embed(text: str) -> list[float]:
    import requests
    r = requests.post(
        "http://localhost:11434/api/embeddings",
        json={"model": "nomic-embed-text", "prompt": text}, timeout=30,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def _search_8k_filings(ticker: str, days: int = 14) -> list[dict]:
    """Recent 8-K (material event) filings for a ticker via EDGAR full-text
    search - same endpoint/pattern edgar_insider.py already uses for Form 4."""
    import requests
    from datetime import datetime, timedelta

    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    until = datetime.now().strftime("%Y-%m-%d")
    url = (
        f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom"
        f"&startdt={since}&enddt={until}&forms=8-K"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return []
        return r.json().get("hits", {}).get("hits", [])
    except Exception:
        return []


def _fetch_filing_text(hit: dict) -> str | None:
    """Fetch + strip HTML/XBRL for one EDGAR full-text-search hit's primary document."""
    import requests
    from bs4 import BeautifulSoup

    src  = hit.get("_source", {})
    adsh = src.get("adsh", "")
    ciks = src.get("ciks", [])
    doc_id_raw = hit.get("_id", "")
    if not adsh or not ciks or ":" not in doc_id_raw:
        return None

    issuer_cik  = ciks[-1].lstrip("0") or "0"   # issuer is consistently last, per edgar_insider.py
    filename    = doc_id_raw.split(":", 1)[1]
    adsh_nodash = adsh.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{issuer_cik}/{adsh_nodash}/{filename}"

    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        return soup.get_text(separator=" ", strip=True)
    except Exception:
        return None


def run_filing_embed(tickers: list[str], days: int = 14) -> dict:
    """
    For each ticker: find recent 8-K filings, skip ones already embedded
    (checked against ChromaDB before any fetch), fetch + chunk + embed
    the rest.
    """
    t0 = time.time()
    collection = _get_chroma_collection()

    total_found = total_skipped = total_embedded = total_failed = total_wrong_issuer = 0

    for ticker in tickers:
        real_cik = _resolve_cik(ticker)
        if not real_cik:
            print(f"[FilingEmbed] {ticker}: could not resolve a real CIK, skipping")
            continue

        for hit in _search_8k_filings(ticker, days=days):
            src  = hit.get("_source", {})
            adsh = src.get("adsh", "")
            ciks = src.get("ciks", [])
            doc_id_raw = hit.get("_id", "")
            if not adsh or ":" not in doc_id_raw:
                continue

            # Structural check, not a text-match guess - see module
            # docstring for the real false-positive this caught live
            # (a search for "NVDA" matching an unrelated Canadian
            # clearinghouse options-listing document).
            hit_ciks = {c.lstrip("0") or "0" for c in ciks}
            if real_cik not in hit_ciks:
                total_wrong_issuer += 1
                continue

            filename = doc_id_raw.split(":", 1)[1]
            doc_id   = f"{adsh}:{filename}"
            total_found += 1

            # Incremental check BEFORE any fetch/parse/embed work - the
            # whole point of item 4's "only new documents" requirement.
            existing = collection.get(ids=[f"{doc_id}:0"])
            if existing.get("ids"):
                total_skipped += 1
                continue

            text = _fetch_filing_text(hit)
            if not text or len(text) < MIN_TEXT_LEN:
                total_failed += 1
                continue

            try:
                for i, chunk in enumerate(_chunk_text(text)):
                    collection.add(
                        ids=[f"{doc_id}:{i}"],
                        embeddings=[_embed(chunk)],
                        documents=[chunk],
                        metadatas=[{
                            "ticker": ticker, "form": src.get("form", "8-K"),
                            "file_date": src.get("file_date", ""),
                            "accession": adsh, "chunk_index": i,
                        }],
                    )
                total_embedded += 1
            except Exception as e:
                print(f"[FilingEmbed] {ticker} {doc_id} embed failed: {e}")
                total_failed += 1

    elapsed = round(time.time() - t0, 1)
    print(f"[FilingEmbed] Done in {elapsed}s — {len(tickers)} tickers, "
          f"{total_wrong_issuer} false-positive text matches rejected, "
          f"{total_found} real filings found, {total_skipped} already embedded, "
          f"{total_embedded} newly embedded, {total_failed} failed")

    return {
        "agent": "search_filing_embed",
        "tickers_scanned": len(tickers),
        "false_positive_matches_rejected": total_wrong_issuer,
        "filings_found": total_found,
        "filings_skipped_already_embedded": total_skipped,
        "filings_embedded": total_embedded,
        "filings_failed": total_failed,
        "elapsed": elapsed,
    }
