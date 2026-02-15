"""
Market hours utilities and time helpers.

All times are US/Eastern. Market hours:
  Pre-market: 4:00 AM - 9:30 AM ET
  Regular:    9:30 AM - 4:00 PM ET
  After-hours: 4:00 PM - 8:00 PM ET
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytz

ET = pytz.timezone("US/Eastern")

# Market time boundaries
PREMARKET_OPEN = time(4, 0)
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def now_et() -> datetime:
    """Current time in US/Eastern timezone."""
    return datetime.now(ET)


def is_premarket() -> bool:
    """True if current ET time is between 4:00 AM and 9:30 AM."""
    t = now_et().time()
    return PREMARKET_OPEN <= t < MARKET_OPEN


def is_market_open() -> bool:
    """
    True if current ET time is between 9:30 AM and 4:00 PM on a weekday.

    Does NOT account for market holidays. For production use,
    integrate with exchange_calendars or pandas_market_calendars.
    """
    now = now_et()
    # Monday=0, Sunday=6
    if now.weekday() >= 5:
        return False
    t = now.time()
    return MARKET_OPEN <= t < MARKET_CLOSE


def is_near_close(minutes_before: int = 10) -> bool:
    """
    True if within `minutes_before` of 4:00 PM ET.

    Used to trigger the hard time exit (default: 3:50 PM).
    """
    now = now_et()
    if now.weekday() >= 5:
        return False
    close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    threshold = close_dt - timedelta(minutes=minutes_before)
    return threshold <= now < close_dt


def time_until_market_open() -> float:
    """
    Seconds until next market open (9:30 AM ET).

    Returns 0.0 if market is already open.
    Returns negative if after market close (next open is tomorrow).
    """
    now = now_et()
    t = now.time()

    if MARKET_OPEN <= t < MARKET_CLOSE and now.weekday() < 5:
        return 0.0

    # Calculate next open
    next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if t >= MARKET_OPEN or now.weekday() >= 5:
        # Move to next business day
        next_open += timedelta(days=1)
        while next_open.weekday() >= 5:
            next_open += timedelta(days=1)

    return (next_open - now).total_seconds()


def parse_time_et(time_str: str) -> time:
    """
    Parse a time string (HH:MM) into a time object.

    Args:
        time_str: Time in HH:MM format (24-hour).

    Returns:
        time object.
    """
    parts = time_str.split(":")
    return time(int(parts[0]), int(parts[1]))


def is_past_exit_time(exit_time_str: str) -> bool:
    """
    Check if current ET time is past the hard exit time.

    Args:
        exit_time_str: Exit time in HH:MM format (e.g., "15:50").
    """
    exit_time = parse_time_et(exit_time_str)
    return now_et().time() >= exit_time


def format_currency(amount: float) -> str:
    """Format a dollar amount with sign and commas."""
    if amount >= 0:
        return f"${amount:,.2f}"
    return f"-${abs(amount):,.2f}"


def format_pct(value: float) -> str:
    """Format a percentage value."""
    return f"{value:+.2f}%"
