"""
Retrieval Library (MULTIAGENT_MIGRATION.md items 14-17).

Shared code, not a service — imported directly by Bull/Bear (item 16),
same as any other module in this codebase. Two independent lookups:

  similar_outcomes()  (item 14) - plain SQL over paper_trade_context +
    daily_recommendations, matched on the SAME categorical buckets
    Phase 6's weekly review (app/learning/weekly_review.py) already
    computes. No embeddings - this is real historical win-rate/PnL
    for candidates that shared the same bucket label, not a vibe.

  semantic_search()   (item 15) - query-time embed (same local Ollama
    model, nomic-embed-text, already used in agents/filing_embed.py)
    + search against the ChromaDB corpus Search Agent builds (item 4).
    Bull/Bear only ever QUERY this corpus here - they never build or
    write to it, that's Search Agent's job alone.

build_retrieval_context() combines both into the one text block
run_bull_bear_debate() (bull_bear_agents.py) already had a
retrieval_context parameter waiting for, since items 11-13 - wiring
this in is item 16. Item 17 ("actually wire up ChromaDB for the first
time") is closed out by this module's semantic_search() being the
first REAL reader of the collection agents/filing_embed.py writes -
the write side existed since item 4, this is the read side making the
round trip real.

Bucket dimension note: weekly_review.py's strategy_rule bucket
(which_strategy_rule_fired) is an LLM-assigned label from the SAME
kind of decision Bull/Bear is about to make - it can't be known before
that decision happens, so it's not usable for a PRE-decision retrieval
lookup and is deliberately excluded here. oi_persistence and
window_length are used as-is. iv_trend's historical side still uses
the real stored iv_5day_trend field; the live candidate side
approximates it from iv_expansion.py's iv_exp_signal (a different but
related "is IV rising" measure) since real iv_5day_trend isn't
computed for a not-yet-decided candidate — documented here, not
silently assumed equivalent.
"""


def _bucket_dimensions_for_candidate(enriched: dict, routing: dict) -> dict[str, str | None]:
    """
    What bucket label this live candidate would fall into, for the
    dimensions computable before Bull/Bear decides. None = not
    computable / excluded from this particular lookup.
    """
    oi_max_days = enriched.get("oi_max_days")
    oi_bucket = None
    if oi_max_days is not None:
        oi_bucket = "10plus_days" if oi_max_days >= 10 else "under_10_days"

    iv_exp_signal = enriched.get("iv_exp_signal")
    iv_bucket = None
    if iv_exp_signal and iv_exp_signal != "INSUFFICIENT_HISTORY":
        iv_bucket = "expanding" if iv_exp_signal == "EXPANDING" else "flat_or_contracting"

    window_bucket = None
    if routing.get("expiry"):   # routing succeeded — real dte known
        dte = 21   # rough routing horizon, same fixed value prediction_agent.py uses
        window_bucket = "short_leq7" if dte <= 7 else "mid_8_30" if dte <= 30 else "long_gt30"

    return {
        "oi_persistence": oi_bucket,
        "iv_trend":       iv_bucket,
        "window_length":  window_bucket,
    }


def similar_outcomes(enriched: dict, routing: dict, user_id: str, lookback_days: int = 90) -> dict:
    """
    Item 14. For each bucket dimension computable for this live
    candidate, look up real historical daily_recommendations +
    paper_trade_context rows sharing that same label and return their
    aggregate win rate / avg PnL - reusing weekly_review.py's own
    _bucket_stats() aggregation, not a second copy of it.
    """
    from datetime import date, timedelta
    from sqlalchemy import text
    from app.db.session import get_session
    from app.learning.weekly_review import _bucket_stats

    dims = _bucket_dimensions_for_candidate(enriched, routing)
    since = date.today() - timedelta(days=lookback_days)

    column_map = {
        "oi_persistence": ("oi_max_days", None),   # handled specially below (range, not equality)
        "iv_trend":       ("iv_5day_trend", None),
        "window_length":  ("trading_window_days", None),
    }

    results = {}
    with get_session() as s:
        for dim_name, label in dims.items():
            if label is None:
                results[dim_name] = {"label": None, "stats": None, "note": "not computable pre-decision"}
                continue

            if dim_name == "oi_persistence":
                where_extra = "ptc.oi_max_days >= 10" if label == "10plus_days" else "ptc.oi_max_days < 10"
                rows = s.execute(text(f"""
                    SELECT dr.was_correct, dr.actual_pnl_pct
                    FROM daily_recommendations dr
                    JOIN paper_trade_context ptc ON ptc.recommendation_id = dr.id
                    WHERE dr.user_id = :uid AND dr.date >= :since
                      AND dr.was_correct IS NOT NULL
                      AND (dr.excluded_from_stats IS NULL OR dr.excluded_from_stats = FALSE)
                      AND ptc.oi_max_days IS NOT NULL AND {where_extra}
                """), {"uid": user_id, "since": since}).fetchall()

            elif dim_name == "iv_trend":
                where_extra = "ptc.iv_5day_trend > 5.0" if label == "expanding" else "ptc.iv_5day_trend <= 5.0"
                rows = s.execute(text(f"""
                    SELECT dr.was_correct, dr.actual_pnl_pct
                    FROM daily_recommendations dr
                    JOIN paper_trade_context ptc ON ptc.recommendation_id = dr.id
                    WHERE dr.user_id = :uid AND dr.date >= :since
                      AND dr.was_correct IS NOT NULL
                      AND (dr.excluded_from_stats IS NULL OR dr.excluded_from_stats = FALSE)
                      AND ptc.iv_5day_trend IS NOT NULL AND {where_extra}
                """), {"uid": user_id, "since": since}).fetchall()

            else:  # window_length
                if label == "short_leq7":
                    where_extra = "ptc.trading_window_days <= 7"
                elif label == "mid_8_30":
                    where_extra = "ptc.trading_window_days BETWEEN 8 AND 30"
                else:
                    where_extra = "ptc.trading_window_days > 30"
                rows = s.execute(text(f"""
                    SELECT dr.was_correct, dr.actual_pnl_pct
                    FROM daily_recommendations dr
                    JOIN paper_trade_context ptc ON ptc.recommendation_id = dr.id
                    WHERE dr.user_id = :uid AND dr.date >= :since
                      AND dr.was_correct IS NOT NULL
                      AND (dr.excluded_from_stats IS NULL OR dr.excluded_from_stats = FALSE)
                      AND ptc.trading_window_days IS NOT NULL AND {where_extra}
                """), {"uid": user_id, "since": since}).fetchall()

            results[dim_name] = {"label": label, "stats": _bucket_stats(rows), "note": None}

    return results


def semantic_search(query_text: str, ticker: str | None = None, n_results: int = 3) -> list[dict]:
    """
    Item 15. Query-time embed + search against the ChromaDB corpus
    Search Agent builds (agents/filing_embed.py, item 4). Read-only -
    never writes to the collection.
    """
    try:
        import chromadb
        from app.agents.filing_embed import _embed
    except Exception as e:
        print(f"[RetrievalLibrary] semantic_search unavailable: {e}")
        return []

    try:
        client = chromadb.HttpClient(host="localhost", port=8000)
        collection = client.get_collection("sec_filings")
    except Exception as e:
        print(f"[RetrievalLibrary] ChromaDB collection unavailable: {e}")
        return []

    try:
        query_embedding = _embed(query_text)
        where = {"ticker": ticker.upper()} if ticker else None
        result = collection.query(
            query_embeddings=[query_embedding], n_results=n_results, where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        print(f"[RetrievalLibrary] semantic_search query failed: {e}")
        return []

    docs  = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    return [
        {"text": d, "metadata": m, "distance": dist}
        for d, m, dist in zip(docs, metas, dists)
    ]


def build_retrieval_context(enriched: dict, routing: dict, user_id: str) -> str:
    """
    Item 16: combines both lookups into the text block
    bull_bear_agents.py's retrieval_context parameter expects.
    """
    ticker = enriched.get("ticker", routing.get("ticker", ""))
    lines = []

    try:
        outcomes = similar_outcomes(enriched, routing, user_id)
        for dim, r in outcomes.items():
            if r["label"] is None or r["stats"] is None:
                continue
            st = r["stats"]
            if not st["sufficient_sample"]:
                lines.append(f"- {dim}={r['label']}: only {st['sample_size']} historical trades — insufficient sample")
            else:
                lines.append(
                    f"- {dim}={r['label']}: {st['win_rate']}% win rate, "
                    f"{st['avg_pnl_pct']:+.1f}% avg P&L over {st['sample_size']} historical trades"
                )
    except Exception as e:
        print(f"[RetrievalLibrary] similar_outcomes failed: {e}")

    try:
        query = f"{ticker} earnings guidance material events recent filings"
        hits = semantic_search(query, ticker=ticker, n_results=2)
        for h in hits:
            meta = h.get("metadata", {})
            snippet = (h.get("text") or "")[:200].replace("\n", " ")
            lines.append(f"- {meta.get('form','?')} filed {meta.get('file_date','?')}: {snippet}...")
    except Exception as e:
        print(f"[RetrievalLibrary] semantic_search failed: {e}")

    return "\n".join(lines) if lines else "No similar historical outcomes or relevant filings found."
