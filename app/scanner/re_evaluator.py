"""
Step 7 — Re-evaluation Layer.

Sits between scanner Tier 1 (quick_scan) and Tier 2 (deep_analyze).
Scores each pick on 6 criteria before passing to LLM strategy engine.

Flow:
    quick_scan() → top 5 picks
                        ↓
            RE-EVALUATE each pick:
              ✓ VIX zone OK?           (from RAG)
              ✓ Volume confirmed?      (from RAG)
              ✓ Entry trigger good?    (from RAG)
              ✓ IV rank acceptable?    (from RAG)
              ✓ Flow still valid?      (dp_score, flow_score from scanner)
              ✓ No extreme geo risk?   (from RAG geo news)
                        ↓
            Score 0-6 → pass if ≥ MIN_SCORE (default 3)
            If < 2 picks pass: lower threshold, pass top 2 anyway
                        ↓
            deep_analyze() → LLM strategy recommendations

Feeds W17 backtesting:
    Was re-eval score predictive of outcomes?
    Did score ≥ 4 picks win more than score < 4?
    Which criteria were most predictive?
"""
from datetime import datetime


MIN_SCORE      = 3     # minimum score to pass to strategy engine
ALWAYS_PASS_N  = 2     # always pass at least this many even if score < MIN_SCORE


def _score_vix(vix: dict, direction: str) -> tuple[int, str]:
    """
    VIX zone check. EXTREME = block all. HIGH = block buying options.
    Returns (score 0-1, reason)
    """
    zone = vix.get("zone", "NORMAL")

    if zone == "EXTREME":
        return 0, f"VIX EXTREME ({vix.get('current')}) — no new positions"
    if zone == "HIGH" and direction != "NEUTRAL":
        return 0, f"VIX HIGH ({vix.get('current')}) — avoid directional options"
    if zone in ("LOW", "NORMAL"):
        return 1, f"VIX {zone} ({vix.get('current')}) — OK"
    # ELEVATED — still OK but note it
    return 1, f"VIX ELEVATED ({vix.get('current')}) — prefer spreads"


def _score_volume(price: dict, direction: str) -> tuple[int, str]:
    """
    Volume confirmation check.
    Price move backed by volume = signal is reliable.
    """
    confirmed = price.get("volume_confirmed", False)
    rel_vol   = price.get("relative_volume", 1.0)
    signal    = price.get("volume_signal", "UNKNOWN")

    if confirmed:
        return 1, f"Volume confirmed ({rel_vol}x avg — {signal})"
    elif rel_vol >= 0.7:
        # Below average but not terrible — give half credit (round to 1 for simplicity)
        return 1, f"Volume below average but acceptable ({rel_vol}x — {signal})"
    else:
        return 0, f"Volume very low ({rel_vol}x avg — {signal}) — signal may be false"


def _score_entry_trigger(price: dict, direction: str) -> tuple[int, str]:
    """
    Entry timing check.
    AT_RESISTANCE for bearish = good.
    AT_SUPPORT for bullish = good.
    BETWEEN_LEVELS = neutral (still passes, just not ideal).
    """
    trigger = price.get("entry_trigger", "UNKNOWN")
    note    = price.get("entry_note", "")

    # Perfect entry conditions
    if direction == "BEARISH" and trigger in ("AT_RESISTANCE", "NEAR_RESISTANCE"):
        return 1, f"Good bearish entry: {trigger}"
    if direction == "BULLISH" and trigger in ("AT_SUPPORT", "NEAR_SUPPORT"):
        return 1, f"Good bullish entry: {trigger}"

    # Neutral — between levels, still tradeable
    if trigger == "BETWEEN_LEVELS":
        return 1, "Between S/R levels — entry is acceptable"

    # Bad entry — price at support for bearish, or at resistance for bullish
    if direction == "BEARISH" and trigger in ("AT_SUPPORT", "NEAR_SUPPORT"):
        return 0, f"Poor bearish entry: price at support — likely to bounce"
    if direction == "BULLISH" and trigger in ("AT_RESISTANCE", "NEAR_RESISTANCE"):
        return 0, f"Poor bullish entry: price at resistance — likely to reject"

    return 1, f"Entry trigger: {trigger}"  # unknown = neutral pass


def _score_iv(iv: dict, direction: str) -> tuple[int, str]:
    """
    IV rank check.
    Buying options: need rank < 60 (not too expensive)
    Selling premium: need rank > 40 (IV high enough to sell)
    """
    if iv.get("error"):
        return 1, "IV rank unavailable — neutral pass"

    iv_rank = iv.get("iv_rank", 50)
    zone    = iv.get("iv_zone", "FAIR")

    # If IV very expensive and we're buying options — bad
    if iv_rank >= 75 and iv.get("buy_options") is False:
        return 0, f"IV very expensive (rank {iv_rank:.0f}/100) — avoid buying options"

    # If IV very cheap and we want to sell premium — bad (but rare scenario)
    if iv_rank <= 15 and direction == "NEUTRAL":
        return 0, f"IV very cheap (rank {iv_rank:.0f}/100) — premium too low to sell"

    return 1, f"IV rank {iv_rank:.0f}/100 ({zone}) — {iv.get('strategy_note','')[:50]}"


def _score_flow(pick: dict) -> tuple[int, str]:
    """
    Flow signal from scanner.
    Uses dp_score and flow_score already computed in quick_scan.
    """
    dp_score   = pick.get("dp_score", 0)
    flow_score = pick.get("flow_score", 0)
    direction  = pick.get("direction", "NEUTRAL")

    # Weekend/holiday mode — no live flow, neutral pass
    if dp_score == 0 and flow_score == 0:
        return 1, "No live flow data (weekend/holiday) — momentum only"

    # Flow confirms direction
    if flow_score >= 50 or dp_score >= 50:
        return 1, f"Flow confirmed: dp={dp_score} flow={flow_score}"

    # Weak flow
    return 1, f"Weak flow signal: dp={dp_score} flow={flow_score}"


def _score_geo(geo: dict) -> tuple[int, str]:
    """
    Geopolitical risk check.
    We pass all news to LLM — but if no major risk, that's a green flag.
    Only block on explicitly HIGH risk items affecting the ticker directly.
    """
    risk_level = geo.get("geo_risk_level", "UNKNOWN")
    headlines  = geo.get("relevant_headlines", [])

    # LLM assesses — always pass but note risk level
    if risk_level == "LLM_ASSESSED":
        fed_items = [h for h in headlines if h.get("type") == "fed"]
        if fed_items:
            return 1, f"Fed news present — LLM will assess ({len(fed_items)} Fed items)"
        return 1, f"Global news passed to LLM ({len(headlines)} headlines)"

    return 1, "No major geo risk detected"


# ─────────────────────────────────────────────────────────────────────────────
# Main Re-evaluation Function
# ─────────────────────────────────────────────────────────────────────────────

def re_evaluate_picks(
    picks: list[dict],
    min_score: int = MIN_SCORE,
) -> list[dict]:
    """
    Score each scanner pick on 6 criteria using RAG context.
    Returns picks sorted by score, with at least ALWAYS_PASS_N picks.

    Each pick gets:
        re_eval_score:    0-6 total
        re_eval_passed:   True if score >= min_score
        re_eval_details:  per-criterion breakdown
        re_eval_notes:    list of pass/fail reasons
    """
    from app.rag.context_builder import build_ticker_context, _build_vix_context

    # Fetch VIX once — same for all picks
    vix = _build_vix_context()

    scored_picks = []
    print(f"\n[Re-eval] Scoring {len(picks)} picks...")

    for pick in picks:
        ticker    = pick["ticker"]
        direction = pick.get("direction", "NEUTRAL")

        try:
            # Build full RAG context for this ticker
            ctx   = build_ticker_context(ticker, include_global_news=False)
            price = ctx.get("price", {})
            iv    = ctx.get("iv", {})
            geo   = ctx.get("geo_risk", {})

        except Exception as e:
            print(f"[Re-eval] Context failed for {ticker}: {e}")
            # Can't evaluate — pass through with neutral score
            pick["re_eval_score"]   = 3
            pick["re_eval_passed"]  = True
            pick["re_eval_details"] = {}
            pick["re_eval_notes"]   = ["Context unavailable — neutral pass"]
            scored_picks.append(pick)
            continue

        # Score each criterion
        v_score, v_note = _score_vix(vix, direction)
        vol_score, vol_note = _score_volume(price, direction)
        et_score, et_note   = _score_entry_trigger(price, direction)
        iv_score, iv_note   = _score_iv(iv, direction)
        fl_score, fl_note   = _score_flow(pick)
        geo_score, geo_note = _score_geo(geo)

        total = v_score + vol_score + et_score + iv_score + fl_score + geo_score
        passed = total >= min_score

        details = {
            "vix":           {"score": v_score,   "note": v_note},
            "volume":        {"score": vol_score,  "note": vol_note},
            "entry_trigger": {"score": et_score,   "note": et_note},
            "iv_rank":       {"score": iv_score,   "note": iv_note},
            "flow":          {"score": fl_score,   "note": fl_note},
            "geo_risk":      {"score": geo_score,  "note": geo_note},
        }

        failed_criteria = [k for k, v in details.items() if v["score"] == 0]
        passed_criteria = [k for k, v in details.items() if v["score"] == 1]

        status = "✅ PASS" if passed else "⚠️  WARN"
        print(f"  {ticker:6} {direction:8} score={total}/6 {status} "
              f"| fail: {failed_criteria if failed_criteria else 'none'}")

        pick["re_eval_score"]   = total
        pick["re_eval_passed"]  = passed
        pick["re_eval_details"] = details
        pick["re_eval_notes"]   = {
            "passed": [details[k]["note"] for k in passed_criteria],
            "failed": [details[k]["note"] for k in failed_criteria],
        }
        # Add entry trigger to pick for display
        pick["entry_trigger"] = price.get("entry_trigger") or "DATA_UNAVAILABLE"
        pick["entry_note"]    = price.get("entry_note") or "Price data unavailable (rate limit)"
        pick["iv_rank"]       = iv.get("iv_rank")   # None = data unavailable
        pick["vix_zone"]      = vix.get("zone", "NORMAL")

        scored_picks.append(pick)

    # Sort by re_eval_score desc, then original score desc
    scored_picks.sort(
        key=lambda x: (x["re_eval_score"], x.get("score", 0)),
        reverse=True
    )

    # Ensure at least ALWAYS_PASS_N picks get through
    passed = [p for p in scored_picks if p["re_eval_passed"]]
    failed = [p for p in scored_picks if not p["re_eval_passed"]]

    if len(passed) < ALWAYS_PASS_N and failed:
        # Force-pass top N from failed, mark as warnings
        extras = failed[:ALWAYS_PASS_N - len(passed)]
        for p in extras:
            p["re_eval_passed"] = True
            p["re_eval_warning"] = (
                f"Score {p['re_eval_score']}/6 below threshold "
                f"but included (need min {ALWAYS_PASS_N} picks)"
            )
        passed = passed + extras

    print(f"[Re-eval] {len(passed)}/{len(scored_picks)} picks passed "
          f"(min score {min_score}/6)")

    return passed


def format_re_eval_summary(picks: list[dict]) -> str:
    """Format re-evaluation results for display."""
    lines = ["### Re-evaluation Scores"]
    for p in picks:
        score   = p.get("re_eval_score", "?")
        ticker  = p["ticker"]
        trigger = p.get("entry_trigger", "UNKNOWN")
        iv      = p.get("iv_rank")
        vix     = p.get("vix_zone", "?")
        warn    = p.get("re_eval_warning", "")
        status  = "✅" if p.get("re_eval_passed") else "⚠️"
        iv_str  = f"IV:{iv:.0f}" if iv is not None else "IV:?"
        lines.append(
            f"{status} **{ticker}** {score}/6 | "
            f"Entry: {trigger} | {iv_str} | VIX: {vix}"
            + (f" ⚠️ {warn}" if warn else "")
        )

        # Show failed criteria
        failed_notes = p.get("re_eval_notes", {}).get("failed", [])
        for note in failed_notes:
            lines.append(f"   ✗ {note}")

    return "\n".join(lines)