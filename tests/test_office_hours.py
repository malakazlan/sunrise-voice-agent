from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

import pytest

from app.config import Settings
from app.domain.clinic import CLINIC_TZ
from app.domain.office_hours import clinic_now, is_open


def clinic_time(
    year: int, month: int, day: int, hour: int, minute: int, second: int = 0, micro: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=CLINIC_TZ)


def utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    assert parsed.utcoffset() is not None
    return parsed


def wrong_answers(rows: list[tuple[datetime, bool]]) -> list[tuple[str, bool]]:
    return [(at.isoformat(), expected) for at, expected in rows if is_open(at) is not expected]


def test_boundaries() -> None:
    # Tuesday 2026-11-24, clinic time.
    rows = [
        (clinic_time(2026, 11, 24, 8, 59), False),
        (clinic_time(2026, 11, 24, 8, 59, 59, 999999), False),
        (clinic_time(2026, 11, 24, 9, 0), True),
        (clinic_time(2026, 11, 24, 16, 59), True),
        (clinic_time(2026, 11, 24, 16, 59, 59, 999999), True),
        (clinic_time(2026, 11, 24, 17, 0), False),
    ]

    assert wrong_answers(rows) == []


def test_weekends_closed() -> None:
    rows = [
        (clinic_time(2026, 11, 28, 11, 0), False),  # Saturday
        (clinic_time(2026, 11, 29, 11, 0), False),  # Sunday
        (utc("2026-11-23T03:00:00Z"), False),  # Sunday 22:00 clinic time, Monday in UTC
        (clinic_time(2026, 11, 27, 16, 30), True),  # Friday
    ]
    for hour, minute in [(0, 0), (9, 0), (12, 0), (16, 59), (23, 59)]:
        rows.append((clinic_time(2026, 11, 28, hour, minute), False))
        rows.append((clinic_time(2026, 11, 29, hour, minute), False))

    assert wrong_answers(rows) == []


def test_utc_inputs_and_dst() -> None:
    rows = [
        (utc("2026-11-24T14:00:00Z"), True),  # 09:00 EST
        (utc("2026-11-24T13:59:00Z"), False),
        (utc("2026-07-01T13:00:00Z"), True),  # 09:00 EDT
        (utc("2026-07-01T12:59:00Z"), False),
        (utc("2026-11-02T14:00:00Z"), True),  # Monday after clocks go back, 09:00 EST
        (utc("2026-11-02T13:59:00Z"), False),
        (utc("2026-03-09T13:00:00Z"), True),  # Monday after clocks go forward, 09:00 EDT
        (utc("2026-03-09T12:59:00Z"), False),
        (utc("2026-07-01T21:00:00Z"), False),  # 17:00 EDT
        (utc("2026-07-01T20:59:00Z"), True),
        (utc("2026-11-24T09:00:00-05:00"), True),  # an offset that is not UTC
        (utc("2026-11-24T09:00:00+01:00"), False),  # 03:00 clinic time
    ]

    assert wrong_answers(rows) == []


def test_year_boundary_and_leap_day() -> None:
    rows = [
        (clinic_time(2026, 12, 31, 10, 0), True),  # Thursday
        (clinic_time(2027, 1, 1, 10, 0), True),  # Friday
        (clinic_time(2027, 1, 2, 10, 0), False),  # Saturday
        (utc("2027-01-02T03:00:00Z"), False),  # Friday 22:00 clinic time, Saturday in UTC
        (utc("2027-01-01T14:00:00Z"), True),  # Friday 09:00 EST
        (clinic_time(2028, 2, 29, 10, 0), True),  # Tuesday, leap day
        (clinic_time(2032, 2, 29, 10, 0), False),  # Sunday, leap day
    ]

    assert wrong_answers(rows) == []


class NoOffset(tzinfo):
    """A tzinfo that is set but gives no offset: the datetime is still naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


@dataclass(frozen=True)
class FixedClock:
    simulated_now: datetime | None


def test_naive_datetime_is_rejected() -> None:
    for naive in (datetime(2026, 11, 24, 10, 0), datetime(2026, 11, 24, 10, 0, tzinfo=NoOffset())):
        with pytest.raises(ValueError, match="timezone"):
            is_open(naive)


@pytest.mark.usefixtures("offline_env")
def test_simulated_now_is_honoured_in_demo() -> None:
    settings = Settings(_env_file=None, app_env="demo", simulated_now="2026-11-28T16:00:00Z")

    assert clinic_now(settings) == utc("2026-11-28T16:00:00Z")
    assert is_open(clinic_now(settings)) is False


@pytest.mark.usefixtures("offline_env")
def test_clinic_now_is_clinic_time() -> None:
    settings = Settings(_env_file=None, app_env="demo", simulated_now="2026-11-24T03:00:00Z")

    now = clinic_now(settings)

    assert now.tzinfo is CLINIC_TZ
    assert now.date() == date(2026, 11, 23)
    assert (now.hour, now.minute) == (22, 0)
    assert clinic_now(FixedClock(utc("2026-11-24T03:00:00Z"))).tzinfo is CLINIC_TZ


def test_clinic_now_rejects_a_simulated_value_without_a_zone() -> None:
    for naive in (datetime(2026, 11, 24, 10, 0), datetime(2026, 11, 24, 10, 0, tzinfo=NoOffset())):
        with pytest.raises(ValueError, match="timezone"):
            clinic_now(FixedClock(naive))


@pytest.mark.usefixtures("offline_env")
def test_real_clock_when_simulated_now_unset() -> None:
    settings = Settings(_env_file=None, app_env="prod")
    assert settings.simulated_now is None

    before = datetime.now(UTC)
    now = clinic_now(settings)
    after = datetime.now(UTC)

    assert now.tzinfo is CLINIC_TZ
    assert before <= now <= after
    assert clinic_now(FixedClock(None)).tzinfo is CLINIC_TZ
