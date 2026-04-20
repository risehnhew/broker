"""Tests for parse_bar_time."""
from __future__ import annotations

from datetime import datetime

import pytest

from broker.time_utils import parse_bar_time


UTC = "UTC"
NY = "America/New_York"


class TestParseBarTime:
    def test_standard_format(self):
        result = parse_bar_time("20240115 09:30:00", UTC, NY)
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_double_space_format(self):
        result = parse_bar_time("20240115  09:30:00", UTC, NY)
        assert result is not None

    def test_dash_format(self):
        result = parse_bar_time("20240115-09:30:00", UTC, NY)
        assert result is not None

    def test_date_only_returns_none(self):
        # Date-only bars don't carry intraday time → return None (by design)
        result = parse_bar_time("20240115", UTC, NY)
        assert result is None

    def test_unknown_format_returns_none(self):
        result = parse_bar_time("not-a-date", UTC, NY)
        assert result is None

    def test_timezone_conversion_applied(self):
        # 09:30 UTC → 04:30 NY (UTC-5 in January)
        result = parse_bar_time("20240115 09:30:00", UTC, NY)
        assert result is not None
        assert result.hour == 4
        assert result.minute == 30

    def test_unknown_timezone_returns_naive_datetime(self):
        result = parse_bar_time("20240115 09:30:00", "Invalid/Zone", NY)
        assert result is not None
        assert result.tzinfo is None

    def test_empty_string_returns_none(self):
        result = parse_bar_time("", UTC, NY)
        assert result is None

    def test_same_timezone_no_offset(self):
        result = parse_bar_time("20240115 09:30:00", NY, NY)
        assert result is not None
        assert result.hour == 9
        assert result.minute == 30
