from datetime import UTC, date, datetime, timedelta, tzinfo

import pytest

from app.domain.clinic import clinic_date


def test_clinic_date_is_the_new_york_calendar_date() -> None:
    # 02:30 UTC on Nov 25 is 21:30 EST on Nov 24.
    assert clinic_date(datetime(2026, 11, 25, 2, 30, tzinfo=UTC)) == date(2026, 11, 24)
    # In summer the clinic is UTC-4: 03:59 UTC is still Jun 30, 04:00 UTC is Jul 1.
    assert clinic_date(datetime(2026, 7, 1, 3, 59, tzinfo=UTC)) == date(2026, 6, 30)
    assert clinic_date(datetime(2026, 7, 1, 4, 0, tzinfo=UTC)) == date(2026, 7, 1)


def test_clinic_date_refuses_a_naive_time() -> None:
    with pytest.raises(ValueError, match="timezone"):
        clinic_date(datetime(2026, 11, 25, 2, 30))


class NoOffset(tzinfo):
    """A tzinfo that is set but gives no offset: the datetime is still naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


def test_clinic_date_refuses_a_tzinfo_without_an_offset() -> None:
    with pytest.raises(ValueError, match="timezone"):
        clinic_date(datetime(2026, 11, 25, 2, 30, tzinfo=NoOffset()))
