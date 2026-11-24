"""Date range selection for a run."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HST = "Pacific/Honolulu"            # Hawaii: the business, used for the default day window and call_date_hst in the DB
ACCOUNT_TZ_DEFAULT = "America/Los_Angeles"  # the Google Ads account time zone: the report shows call times in it
MAX_DAYS_PER_LOAD = 7


def today_in(tz: str, now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(tz)).date()


def default_range(now: datetime | None = None, lookback: int = 2, tz: str = HST) -> tuple[date, date]:
    """The last ``lookback`` full days in ``tz``: ends yesterday, starts lookback days ago."""
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(ZoneInfo(tz)).date()
    return today - timedelta(days=lookback), today - timedelta(days=1)


def split_range(start: date, end: date, max_days: int = MAX_DAYS_PER_LOAD) -> list[tuple[date, date]]:
    """One (start, end) per page load: the whole range if short, else one per day."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    n_days = (end - start).days + 1
    if n_days <= max_days:
        return [(start, end)]
    return [(start + timedelta(days=i), start + timedelta(days=i)) for i in range(n_days)]


def days_in(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
