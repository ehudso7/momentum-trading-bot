"""
Market hours utilities and time helpers.

All times are US/Eastern. Market hours:
  Pre-market: 4:00 AM - 9:30 AM ET
  Regular:    9:30 AM - 4:00 PM ET
  After-hours: 4:00 PM - 8:00 PM ET
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache

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
    """True if current ET time is between 4:00 AM and 9:30 AM on a trading day."""
    now = now_et()
    if is_market_holiday(now.date()):
        return False
    t = now.time()
    return PREMARKET_OPEN <= t < MARKET_OPEN


def is_market_open() -> bool:
    """
    True if current ET time is between 9:30 AM and market close on a trading day.

    Accounts for weekends, NYSE market holidays, and early close days.
    """
    now = now_et()
    # Monday=0, Sunday=6
    if now.weekday() >= 5:
        return False
    if is_market_holiday(now.date()):
        return False
    t = now.time()
    close = _early_close_time(now.date()) or MARKET_CLOSE
    return MARKET_OPEN <= t < close


def _early_close_time(d: date) -> time | None:
    """
    Return 1:00 PM ET if *d* is an NYSE early-close day, else None.

    NYSE early-close days (1:00 PM ET):
      - Day before Independence Day (July 3, or July 2 if July 3 is weekend)
      - Black Friday (day after Thanksgiving)
      - Christmas Eve (Dec 24, or Dec 23 if Dec 24 is weekend)
    """
    year = d.year
    early_close_dates: list[date] = []

    # Day before Independence Day
    jul4 = _observe(date(year, 7, 4))
    day_before_jul4 = jul4 - timedelta(days=1)
    if day_before_jul4.weekday() < 5:
        early_close_dates.append(day_before_jul4)

    # Black Friday: day after Thanksgiving (4th Thursday in November)
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    black_friday = thanksgiving + timedelta(days=1)
    early_close_dates.append(black_friday)

    # Christmas Eve
    xmas = _observe(date(year, 12, 25))
    day_before_xmas = xmas - timedelta(days=1)
    if day_before_xmas.weekday() < 5:
        early_close_dates.append(day_before_xmas)

    if d in early_close_dates:
        return time(13, 0)
    return None


def is_near_close(minutes_before: int = 10) -> bool:
    """
    True if within `minutes_before` of market close.

    Accounts for early close days (1:00 PM ET).
    Used to trigger the hard time exit (default: 3:50 PM).
    """
    now = now_et()
    if now.weekday() >= 5:
        return False
    if is_market_holiday(now.date()):
        return False
    early = _early_close_time(now.date())
    close_hour = early.hour if early else 16
    close_minute = early.minute if early else 0
    close_dt = now.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
    threshold = close_dt - timedelta(minutes=minutes_before)
    return threshold <= now < close_dt


def time_until_market_open() -> float:
    """
    Seconds until next market open (9:30 AM ET).

    Returns 0.0 if market is already open.
    Skips weekends and NYSE holidays.
    """
    now = now_et()
    t = now.time()

    if (
        MARKET_OPEN <= t < MARKET_CLOSE
        and now.weekday() < 5
        and not is_market_holiday(now.date())
    ):
        return 0.0

    # Calculate next open
    next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if t >= MARKET_OPEN or now.weekday() >= 5 or is_market_holiday(now.date()):
        # Move to next business day
        next_open += timedelta(days=1)
        while next_open.weekday() >= 5 or is_market_holiday(next_open.date()):
            next_open += timedelta(days=1)

    return (next_open - now).total_seconds()


def parse_time_et(time_str: str) -> time:
    """
    Parse a time string (HH:MM) into a time object.

    Args:
        time_str: Time in HH:MM format (24-hour).

    Returns:
        time object.

    Raises:
        ValueError: If the string is not valid HH:MM format.
    """
    if not time_str or ":" not in time_str:
        raise ValueError(f"Invalid time format (expected HH:MM): {time_str!r}")
    parts = time_str.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format (expected HH:MM): {time_str!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid time format (non-numeric): {time_str!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time values (h={h}, m={m}): {time_str!r}")
    return time(h, m)


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


# ---------------------------------------------------------------------------
# NYSE Market Holiday Calendar
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _nyse_holidays(year: int) -> frozenset[date]:
    """
    Compute NYSE market holidays for a given year.

    Holidays observed:
      - New Year's Day (Jan 1)
      - Martin Luther King Jr. Day (3rd Monday in January)
      - Presidents' Day (3rd Monday in February)
      - Good Friday (Friday before Easter)
      - Memorial Day (last Monday in May)
      - Juneteenth (June 19, observed since 2022)
      - Independence Day (July 4)
      - Labor Day (1st Monday in September)
      - Thanksgiving Day (4th Thursday in November)
      - Christmas Day (December 25)

    When a holiday falls on Saturday, the prior Friday is observed.
    When a holiday falls on Sunday, the following Monday is observed.
    """
    holidays: list[date] = []

    # --- Fixed-date holidays (with weekend adjustment) ---
    holidays.append(_observe(date(year, 1, 1)))    # New Year's Day
    if year >= 2022:
        holidays.append(_observe(date(year, 6, 19)))  # Juneteenth
    holidays.append(_observe(date(year, 7, 4)))    # Independence Day
    holidays.append(_observe(date(year, 12, 25)))  # Christmas Day

    # --- Floating holidays ---
    holidays.append(_nth_weekday(year, 1, 0, 3))   # MLK: 3rd Mon in Jan
    holidays.append(_nth_weekday(year, 2, 0, 3))   # Presidents': 3rd Mon in Feb
    holidays.append(_last_weekday(year, 5, 0))      # Memorial: last Mon in May
    holidays.append(_nth_weekday(year, 9, 0, 1))   # Labor: 1st Mon in Sep
    holidays.append(_nth_weekday(year, 11, 3, 4))  # Thanksgiving: 4th Thu in Nov

    # Good Friday (2 days before Easter Sunday)
    holidays.append(_easter(year) - timedelta(days=2))

    return frozenset(holidays)


def is_market_holiday(d: date) -> bool:
    """Check if a date is an NYSE market holiday."""
    if d.weekday() >= 5:
        return True  # weekends are always closed
    return d in _nyse_holidays(d.year)


def _observe(d: date) -> date:
    """Adjust a fixed holiday for weekend observance (NYSE rules)."""
    if d.weekday() == 5:     # Saturday -> Friday
        return d - timedelta(days=1)
    elif d.weekday() == 6:   # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """
    Find the nth occurrence of a weekday in a given month.

    Args:
        weekday: 0=Monday, 3=Thursday, etc.
        n: 1-based (1st, 2nd, 3rd, 4th).
    """
    first_day = date(year, month, 1)
    # Days until first occurrence of target weekday
    days_ahead = (weekday - first_day.weekday()) % 7
    first_occurrence = first_day + timedelta(days=days_ahead)
    return first_occurrence + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Find the last occurrence of a weekday in a given month."""
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    days_back = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=days_back)


def _easter(year: int) -> date:
    """
    Compute Easter Sunday using the Anonymous Gregorian algorithm.

    Valid for any year in the Gregorian calendar.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)
