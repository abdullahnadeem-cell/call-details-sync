from datetime import date, datetime, timezone

import pytest

from src.dates import default_range, split_range


def test_default_is_last_two_full_hst_days():
    # 2026-11-24 08:00 UTC is 2026-11-23 22:00 HST -> yesterday HST is 11-22
    now = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)
    assert default_range(now) == (date(2026, 11, 21), date(2026, 11, 22))
    # after HST midnight the window moves
    now = datetime(2026, 11, 24, 10, 30, tzinfo=timezone.utc)  # 00:30 HST on 11-24
    assert default_range(now) == (date(2026, 11, 22), date(2026, 11, 23))


def test_lookback():
    now = datetime(2026, 11, 24, 16, 30, tzinfo=timezone.utc)  # 06:30 HST 11-24
    assert default_range(now, lookback=1) == (date(2026, 11, 23), date(2026, 11, 23))
    assert default_range(now, lookback=5) == (date(2026, 11, 19), date(2026, 11, 23))
    with pytest.raises(ValueError):
        default_range(now, lookback=0)


def test_split_short_range_is_one_load():
    assert split_range(date(2026, 11, 1), date(2026, 11, 7)) == [(date(2026, 11, 1), date(2026, 11, 7))]
    assert split_range(date(2026, 11, 20), date(2026, 11, 20)) == [(date(2026, 11, 20), date(2026, 11, 20))]


def test_split_long_range_is_one_load_per_day():
    chunks = split_range(date(2026, 11, 1), date(2026, 11, 8))
    assert len(chunks) == 8
    assert chunks[0] == (date(2026, 11, 1), date(2026, 11, 1))
    assert chunks[-1] == (date(2026, 11, 8), date(2026, 11, 8))
    with pytest.raises(ValueError):
        split_range(date(2026, 11, 8), date(2026, 11, 1))
