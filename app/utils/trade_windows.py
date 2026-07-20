"""
Shared trade-parameter validation and expiry-date computation.

Both the options engine (rescan_engine.py) and the stock/horizon engine
(horizon_engine.py) need the SAME logic for validating the 5 user-
supplied inputs and computing a target expiry/review date from "N days
from today," rolled forward past weekends/holidays. Centralized here
so both systems share one implementation instead of two independently
drifting copies — the same "two systems doing almost the same thing
slightly differently" pattern found and fixed elsewhere this session
(recommendations tables, watchlists).
"""
from datetime import date, timedelta


def round_budget(budget: float) -> float:
    """Round to nearest cent — e.g. 300.512 -> 300.51."""
    return round(float(budget), 2)


def validate_trading_window(days: int, trade_type: str) -> int:
    """
    Options: 1-365 days. Stock: 1-730 days.
    Raises rather than silently clamping — the API layer surfaces this
    as a clear validation error instead of quietly substituting a
    value the user didn't ask for.
    """
    days = int(days)
    max_days = 730 if trade_type == "stock" else 365
    if days < 1 or days > max_days:
        raise ValueError(
            f"trading_window_days must be between 1 and {max_days} for "
            f"{trade_type} (got {days})"
        )
    return days


def validate_pct(value: float, field_name: str = "value") -> float:
    """Positive integer only — no decimals, no negatives."""
    value = float(value)
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive number (got {value})")
    return float(int(round(value)))


def compute_target_date(trading_window_days: int) -> str:
    """
    today + trading_window_days, rolled FORWARD to the next open
    trading day if it lands on a weekend or US market holiday.
    Returns 'YYYY-MM-DD'.
    """
    from app.scanner.quick_scan import us_market_holidays

    target = date.today() + timedelta(days=trading_window_days)
    while target.weekday() >= 5 or target in us_market_holidays(target.year):
        target += timedelta(days=1)
    return target.strftime("%Y-%m-%d")


def nearest_friday_to(target_date_str: str) -> str:
    """
    Nearest Friday to a target date — SPY/QQQ have expiries on
    virtually every Friday, so this is a reliable approximation
    specifically for locking their expiry (individual stocks' real
    listed expiries vary and are matched against actual UW contract
    data elsewhere, not approximated this way).
    """
    from datetime import datetime

    target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    days_to_next_fri = (4 - target.weekday()) % 7
    next_fri = target + timedelta(days=days_to_next_fri)
    prev_fri = next_fri - timedelta(days=7) if days_to_next_fri > 0 else next_fri
    if abs((next_fri - target).days) <= abs((target - prev_fri).days):
        return next_fri.strftime("%Y-%m-%d")
    return prev_fri.strftime("%Y-%m-%d")
