"""Tests for market hours utilities and holiday detection."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
import pytz

from trading_bot.utils.helpers import (
    _easter,
    _nth_weekday,
    _last_weekday,
    _nyse_holidays,
    _observe,
    is_market_holiday,
    is_market_open,
    is_near_close,
    is_premarket,
    time_until_market_open,
)

ET = pytz.timezone("US/Eastern")


class TestNYSEHolidays:
    """Test NYSE holiday calendar computation."""

    def test_presidents_day_2026(self):
        """Feb 16 2026 is Presidents' Day — market closed."""
        assert is_market_holiday(date(2026, 2, 16))

    def test_day_after_presidents_day_2026(self):
        """Feb 17 2026 is a regular trading day."""
        assert not is_market_holiday(date(2026, 2, 17))

    def test_christmas_2026_on_friday(self):
        """Dec 25 2026 is a Friday — market closed."""
        assert is_market_holiday(date(2026, 12, 25))

    def test_july_4_2026_saturday_observed_friday(self):
        """July 4 2026 is Saturday — Friday July 3 observed."""
        assert is_market_holiday(date(2026, 7, 3))
        assert not is_market_holiday(date(2026, 7, 6))  # Monday is open

    def test_new_years_2027_on_friday(self):
        """Jan 1 2027 is a Friday — market closed."""
        assert is_market_holiday(date(2027, 1, 1))

    def test_juneteenth_2026(self):
        """June 19 2026 is a Friday — market closed."""
        assert is_market_holiday(date(2026, 6, 19))

    def test_good_friday_2026(self):
        """Good Friday 2026 is April 3."""
        assert is_market_holiday(date(2026, 4, 3))

    def test_thanksgiving_2026(self):
        """Thanksgiving 2026 is Nov 26 (4th Thursday)."""
        assert is_market_holiday(date(2026, 11, 26))

    def test_mlk_day_2026(self):
        """MLK Day 2026 is Jan 19 (3rd Monday)."""
        assert is_market_holiday(date(2026, 1, 19))

    def test_memorial_day_2026(self):
        """Memorial Day 2026 is May 25 (last Monday)."""
        assert is_market_holiday(date(2026, 5, 25))

    def test_labor_day_2026(self):
        """Labor Day 2026 is Sep 7 (1st Monday)."""
        assert is_market_holiday(date(2026, 9, 7))

    def test_regular_monday_not_holiday(self):
        """A regular Monday in March 2026 is not a holiday."""
        assert not is_market_holiday(date(2026, 3, 2))

    def test_weekends_are_holidays(self):
        """Saturdays and Sundays are always market holidays."""
        assert is_market_holiday(date(2026, 2, 14))  # Saturday
        assert is_market_holiday(date(2026, 2, 15))  # Sunday

    def test_ten_holidays_per_year(self):
        """NYSE has exactly 10 holidays per year (since 2022)."""
        for year in [2024, 2025, 2026, 2027]:
            assert len(_nyse_holidays(year)) == 10, f"Expected 10 holidays for {year}"

    def test_all_holidays_are_weekdays(self):
        """All computed holidays should fall on weekdays (Mon-Fri)."""
        for year in [2024, 2025, 2026, 2027]:
            for h in _nyse_holidays(year):
                assert h.weekday() < 5, f"{h} is a weekend"


class TestEaster:
    """Verify Easter computation against known dates."""

    def test_known_easter_dates(self):
        known = {
            2024: date(2024, 3, 31),
            2025: date(2025, 4, 20),
            2026: date(2026, 4, 5),
            2027: date(2027, 3, 28),
            2028: date(2028, 4, 16),
        }
        for year, expected in known.items():
            assert _easter(year) == expected, f"Easter {year}: expected {expected}"


class TestObserve:
    """Test weekend observance rules."""

    def test_saturday_observed_friday(self):
        assert _observe(date(2026, 7, 4)) == date(2026, 7, 3)

    def test_sunday_observed_monday(self):
        # Jan 1 2023 was a Sunday
        assert _observe(date(2023, 1, 1)) == date(2023, 1, 2)

    def test_weekday_unchanged(self):
        assert _observe(date(2026, 1, 1)) == date(2026, 1, 1)


class TestMarketOpen:
    """Test is_market_open with holiday awareness."""

    def _mock_now(self, dt: datetime):
        return patch("trading_bot.utils.helpers.now_et", return_value=dt)

    def test_market_open_regular_day(self):
        # Tuesday Feb 17 2026 at 10:00 AM ET
        dt = ET.localize(datetime(2026, 2, 17, 10, 0))
        with self._mock_now(dt):
            assert is_market_open()

    def test_market_closed_on_holiday(self):
        # Presidents' Day Feb 16 2026 at 10:00 AM ET
        dt = ET.localize(datetime(2026, 2, 16, 10, 0))
        with self._mock_now(dt):
            assert not is_market_open()

    def test_market_closed_before_open(self):
        dt = ET.localize(datetime(2026, 2, 17, 8, 0))
        with self._mock_now(dt):
            assert not is_market_open()

    def test_market_closed_after_close(self):
        dt = ET.localize(datetime(2026, 2, 17, 16, 30))
        with self._mock_now(dt):
            assert not is_market_open()

    def test_market_closed_on_weekend(self):
        dt = ET.localize(datetime(2026, 2, 14, 10, 0))  # Saturday
        with self._mock_now(dt):
            assert not is_market_open()


class TestPremarket:
    """Test is_premarket with holiday awareness."""

    def _mock_now(self, dt: datetime):
        return patch("trading_bot.utils.helpers.now_et", return_value=dt)

    def test_premarket_on_regular_day(self):
        dt = ET.localize(datetime(2026, 2, 17, 7, 0))
        with self._mock_now(dt):
            assert is_premarket()

    def test_no_premarket_on_holiday(self):
        dt = ET.localize(datetime(2026, 2, 16, 7, 0))
        with self._mock_now(dt):
            assert not is_premarket()


class TestNearClose:
    """Test is_near_close with holiday awareness."""

    def _mock_now(self, dt: datetime):
        return patch("trading_bot.utils.helpers.now_et", return_value=dt)

    def test_near_close_on_regular_day(self):
        dt = ET.localize(datetime(2026, 2, 17, 15, 55))
        with self._mock_now(dt):
            assert is_near_close(minutes_before=10)

    def test_not_near_close_on_holiday(self):
        dt = ET.localize(datetime(2026, 2, 16, 15, 55))
        with self._mock_now(dt):
            assert not is_near_close(minutes_before=10)
