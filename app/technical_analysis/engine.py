"""
Technical Analysis Engine (Component C6).

Pure computation — no API calls, no LLM. Takes OHLCV bars from
MarketDataService (Component C3) and computes a full technical profile
in milliseconds per ticker.

Indicators computed:
    Trend:      MA20, MA50, MA200, EMA20
    Momentum:   RSI(14), MACD(12,26,9)
    Volatility: Bollinger Bands(20,2), ATR(14)
    Volume:     Relative volume vs 20-day average
    Levels:     Support (20-bar low), Resistance (20-bar high)
    Summary:    Trend direction, signal (BUY/SELL/NEUTRAL), strength score 0-100

Usage:
    from app.market_data.uw_market_data import get_bars
    from app.technical_analysis.engine import get_technical_profile

    bars = get_bars('NVDA', 1, 'day', '2025-09-01', '2026-06-13')
    profile = get_technical_profile('NVDA', bars)
"""
import pandas as pd
import ta


def _to_df(bars: list[dict]) -> pd.DataFrame:
    """Convert OHLCV bar list to a clean DataFrame.
    Handles both Polygon format (open/high/low/close) and
    UW format (o/h/l/c) automatically.
    """
    # Normalize UW short-key format to standard long-key format
    KEY_MAP = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap"}
    if bars and "c" in bars[0] and "close" not in bars[0]:
        bars = [{KEY_MAP.get(k, k): v for k, v in b.items()} for b in bars]
    df = pd.DataFrame(bars)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


def get_technical_profile(ticker: str, bars: list[dict]) -> dict:
    """
    Compute a full technical profile for a ticker from its OHLCV bars.

    Requires at least 30 bars for basic indicators.
    200+ bars recommended for accurate MA200 and long-term S/R.

    Returns a dict with all indicators plus a plain-English summary.
    """
    if len(bars) < 30:
        return {
            "ticker": ticker,
            "error": f"Need at least 30 bars, got {len(bars)}. "
                     f"Call get_bars() with a wider date range.",
        }

    df = _to_df(bars)
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    n = len(df)

    current_price = float(close.iloc[-1])

    # ── Moving Averages ─────────────────────────────────────────────────────
    ma20  = float(close.rolling(20).mean().iloc[-1])  if n >= 20  else None
    ma50  = float(close.rolling(50).mean().iloc[-1])  if n >= 50  else None
    ma200 = float(close.rolling(200).mean().iloc[-1]) if n >= 200 else None
    ema20 = float(ta.trend.ema_indicator(close, window=20).iloc[-1]) if n >= 20 else None

    above_ma20  = (current_price > ma20)  if ma20  else None
    above_ma50  = (current_price > ma50)  if ma50  else None
    above_ma200 = (current_price > ma200) if ma200 else None

    # ── RSI ──────────────────────────────────────────────────────────────────
    rsi = float(ta.momentum.rsi(close, window=14).iloc[-1]) if n >= 15 else None

    # ── MACD ─────────────────────────────────────────────────────────────────
    macd_line  = ta.trend.macd(close)
    macd_sig   = ta.trend.macd_signal(close)
    macd_hist  = ta.trend.macd_diff(close)
    macd       = float(macd_line.iloc[-1])  if macd_line  is not None else None
    macd_signal= float(macd_sig.iloc[-1])   if macd_sig   is not None else None
    macd_histo = float(macd_hist.iloc[-1])  if macd_hist  is not None else None
    macd_bullish = (macd_histo > 0) if macd_histo is not None else None

    # ── Bollinger Bands ──────────────────────────────────────────────────────
    bb        = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper  = float(bb.bollinger_hband().iloc[-1]) if n >= 20 else None
    bb_middle = float(bb.bollinger_mavg().iloc[-1])  if n >= 20 else None
    bb_lower  = float(bb.bollinger_lband().iloc[-1]) if n >= 20 else None
    bb_pct_b  = float(bb.bollinger_pband().iloc[-1]) if n >= 20 else None
    bb_width  = float(bb.bollinger_wband().iloc[-1]) if n >= 20 else None

    # ── ATR ──────────────────────────────────────────────────────────────────
    atr = float(
        ta.volatility.average_true_range(high, low, close, window=14).iloc[-1]
    ) if n >= 15 else None

    # ── Volume ───────────────────────────────────────────────────────────────
    vol_current = float(volume.iloc[-1])     if not volume.empty else None
    vol_avg_20  = float(volume.rolling(20).mean().iloc[-1]) if n >= 20 else None
    rel_vol     = round(vol_current / vol_avg_20, 2) if (vol_current and vol_avg_20) else None

    # ── Support / Resistance ─────────────────────────────────────────────────
    resistance = float(high.rolling(20).max().iloc[-1]) if n >= 20 else None
    support    = float(low.rolling(20).min().iloc[-1])  if n >= 20 else None

    # ── Trend ────────────────────────────────────────────────────────────────
    ma_score = sum([
        1 if above_ma20  else 0,
        1 if above_ma50  else 0,
        1 if above_ma200 else 0,
    ])
    trend = {3: "STRONG_UPTREND", 2: "UPTREND", 1: "DOWNTREND"}.get(ma_score, "STRONG_DOWNTREND")

    # ── Strength Score (0-100) ───────────────────────────────────────────────
    score = 50  # neutral baseline

    if rsi:
        if   rsi < 30: score += 15   # oversold — bullish
        elif rsi < 50: score += 5
        elif rsi > 70: score -= 15   # overbought — bearish
        elif rsi > 55: score += 5

    if macd_bullish is True:  score += 10
    elif macd_bullish is False: score -= 10

    score += (ma_score - 1.5) * 8   # -12 to +12

    if rel_vol:
        if rel_vol > 2.0: score += 8    # high volume = conviction
        elif rel_vol < 0.5: score -= 5  # low volume = weak move

    score = max(0, min(100, round(score)))
    signal = "BUY" if score >= 65 else ("SELL" if score <= 35 else "NEUTRAL")

    # ── Summary ──────────────────────────────────────────────────────────────
    parts = [
        f"{ticker} @ ${current_price:.2f}",
        f"Trend: {trend}",
        f"RSI(14): {rsi:.1f}"           if rsi     else "",
        f"MACD: {'bullish' if macd_bullish else 'bearish'}" if macd_bullish is not None else "",
        f"RelVol: {rel_vol:.1f}x"       if rel_vol else "",
        f"Signal: {signal} ({score}/100)",
    ]
    summary = " | ".join(p for p in parts if p)

    def r(v, d=2):
        return round(v, d) if v is not None else None

    return {
        "ticker": ticker,
        "bars_count": n,
        "current_price": current_price,

        # Moving averages
        "ma20": r(ma20), "ma50": r(ma50), "ma200": r(ma200), "ema20": r(ema20),
        "above_ma20": above_ma20, "above_ma50": above_ma50, "above_ma200": above_ma200,

        # Momentum
        "rsi_14": r(rsi, 1),
        "macd": r(macd, 4), "macd_signal": r(macd_signal, 4),
        "macd_histogram": r(macd_histo, 4), "macd_bullish": macd_bullish,

        # Volatility
        "bb_upper": r(bb_upper), "bb_middle": r(bb_middle), "bb_lower": r(bb_lower),
        "bb_pct_b": r(bb_pct_b, 3), "bb_width": r(bb_width, 3),
        "atr_14": r(atr),

        # Volume
        "volume_current": int(vol_current) if vol_current else None,
        "volume_avg_20":  int(vol_avg_20)  if vol_avg_20  else None,
        "relative_volume": rel_vol,

        # Levels
        "support": r(support), "resistance": r(resistance),

        # Summary
        "trend": trend, "signal": signal,
        "strength_score": score, "summary": summary,
    }


def analyze_multiple(ticker_bars: dict[str, list[dict]]) -> dict[str, dict]:
    """
    Analyze multiple tickers at once.
    Args:
        ticker_bars: {ticker: bars_list}
    Returns:
        {ticker: technical_profile}
    """
    return {ticker: get_technical_profile(ticker, bars) for ticker, bars in ticker_bars.items()}