"""
Rule-based (NOT LLM) intraday entry-timing signal — 5-min and 15-min.

Sits between the overnight daily thesis (which ticker/direction is
worth watching) and the actual paper trade open (Phase 4) — this does
NOT decide whether to trade, it only logs what the intraday technical
picture looked like at the moment of entry, so Phase 6's weekly review
can later find out empirically which timeframe (if either) actually
correlates with wins.

Observational logging only in this first build, not a gate — every
candidate still gets logged and confirmed regardless of what these
rules say.

Data source note: fetches via unusual_whales.get_ohlc(candle_size="5m"
or "15m") directly, NOT through market_data/uw_market_data.py's
get_bars() wrapper — that wrapper's timespan_map only maps "minute"/
"hour"/"day"/"week" and silently ignores the multiplier argument
entirely, so multiplier=5 would NOT actually fetch 5-minute bars
through it today. Flagged as a separate gap, not fixed here.

Found and fixed a real, blocking bug while building this: get_ohlc()
computed every intraday bar's timestamp as 0 (it only ever read a
"date" field, which daily/weekly candles carry but 5m/15m/1h/1m candles
don't — they carry start_time/end_time instead). Every bar having the
same ts made the function's own sort a no-op, so intraday bars were
silently left in the raw API's actual order — confirmed descending
(newest-first), the opposite of what RSI/MACD assume. Fixed directly in
unusual_whales.py since this data path was unusable for any timeframe-
sensitive calculation otherwise.
"""
import pandas as pd
import ta as ta_lib

CANDLE_SIZE = {"5min": "5m", "15min": "15m"}
BARS_PER_DAY = {"5min": 78, "15min": 26}   # 6.5h regular session / bar size

# Fetch limit: UW's ohlc endpoint accepts up to ~2000-4999 (5000 itself
# 422s) per call; 2000 raw bars comfortably yields 800+ regular-session
# 5-min bars (~10+ trading days) after pre/post-market filtering, ample
# for RSI(14)/MACD(12,26,9) warmup — see module docstring / verification
# notes for the empirical answer on how many days that actually needs.
FETCH_LIMIT = 2000

# Minimum regular-session bars for MACD(12,26,9) to have ANY signal-line
# value at all (26 for the slow EMA + 9 more for the signal line's own
# smoothing) — below this the indicators are structurally undefined,
# not just noisy, so we return an explicit error rather than nulls.
MIN_BARS = 35


def get_intraday_signal(ticker: str, direction: str, timeframe: str) -> dict:
    """
    ticker:    e.g. 'NVDA'
    direction: 'BULLISH' / 'BEARISH' / 'NEUTRAL' (NEUTRAL = credit/iron-
               condor strategies, where direction doesn't apply the same
               way — the directional rule-check is skipped entirely and
               only the raw values are returned, for logging)
    timeframe: '5min' or '15min' — call this twice per candidate (once
               per timeframe), never pick one upfront

    Returns:
        {ticker, direction, timeframe, rsi, macd_histogram,
         price_vs_ema, current_price, ema9, bars_used,
         rules_fired: [...], any_rule_fired: bool}
        or {..., error: "..."} if there wasn't enough real data.
    """
    ticker = ticker.upper()
    base = {"ticker": ticker, "direction": direction, "timeframe": timeframe}

    candle_size = CANDLE_SIZE.get(timeframe)
    if not candle_size:
        return {**base, "error": f"Unknown timeframe '{timeframe}' — expected '5min' or '15min'",
                "rsi": None, "macd_histogram": None, "price_vs_ema": None,
                "rules_fired": [], "any_rule_fired": False}

    from app.options_flow.unusual_whales import get_ohlc
    bars = get_ohlc(ticker, candle_size=candle_size, limit=FETCH_LIMIT)

    if len(bars) < MIN_BARS:
        return {**base, "error": f"Insufficient bars ({len(bars)}, need >= {MIN_BARS})",
                "rsi": None, "macd_histogram": None, "price_vs_ema": None,
                "rules_fired": [], "any_rule_fired": False}

    closes = pd.Series([float(b["c"]) for b in bars])
    current_price = float(closes.iloc[-1])

    rsi_series       = ta_lib.momentum.rsi(closes, window=14)
    macd_hist_series = ta_lib.trend.macd_diff(closes)          # histogram = macd - signal
    ema9_series      = ta_lib.trend.ema_indicator(closes, window=9)

    def _last(series, idx=-1):
        try:
            v = series.iloc[idx]
            return None if pd.isna(v) else float(v)
        except (IndexError, KeyError):
            return None

    rsi           = _last(rsi_series)
    rsi_prev      = _last(rsi_series, -2)
    macd_hist     = _last(macd_hist_series)
    macd_hist_prev = _last(macd_hist_series, -2)
    ema9          = _last(ema9_series)
    price_vs_ema  = round(current_price - ema9, 4) if ema9 is not None else None

    result = {
        **base,
        "rsi":            round(rsi, 1) if rsi is not None else None,
        "macd_histogram": round(macd_hist, 4) if macd_hist is not None else None,
        "price_vs_ema":   price_vs_ema,
        "current_price":  round(current_price, 4),
        "ema9":           round(ema9, 4) if ema9 is not None else None,
        "bars_used":      len(bars),
    }

    if direction == "NEUTRAL":
        # Credit/iron-condor strategies — direction doesn't apply the
        # same way. A range-bound/non-trending read is more relevant
        # here than a directional signal, but this pass doesn't force a
        # rule onto it — just log the raw values.
        result["rules_fired"]   = []
        result["any_rule_fired"] = False
        return result

    rules_fired = []
    if direction == "BULLISH":
        if rsi is not None and rsi_prev is not None and rsi_prev < 30 <= rsi:
            rules_fired.append("rsi_recovering_from_oversold")
        elif (rsi is not None and rsi_prev is not None
              and 40 <= rsi <= 60 and rsi > rsi_prev):
            rules_fired.append("rsi_rising_in_neutral_band")

        if macd_hist is not None and macd_hist > 0:
            if macd_hist_prev is not None and macd_hist_prev <= 0:
                rules_fired.append("macd_histogram_turning_positive")
            else:
                rules_fired.append("macd_histogram_positive")

        if price_vs_ema is not None and price_vs_ema > 0:
            rules_fired.append("price_above_ema9")

    elif direction == "BEARISH":
        if rsi is not None and rsi_prev is not None and rsi_prev > 70 >= rsi:
            rules_fired.append("rsi_falling_from_overbought")
        elif (rsi is not None and rsi_prev is not None
              and 40 <= rsi <= 60 and rsi < rsi_prev):
            rules_fired.append("rsi_falling_in_neutral_band")

        if macd_hist is not None and macd_hist < 0:
            if macd_hist_prev is not None and macd_hist_prev >= 0:
                rules_fired.append("macd_histogram_turning_negative")
            else:
                rules_fired.append("macd_histogram_negative")

        if price_vs_ema is not None and price_vs_ema < 0:
            rules_fired.append("price_below_ema9")

    result["rules_fired"]    = rules_fired
    result["any_rule_fired"] = len(rules_fired) > 0
    return result
