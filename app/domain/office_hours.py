from datetime import datetime, time
from typing import Protocol

from app.domain.clinic import CLINIC_TZ

OPENS = time(9, 0)
CLOSES = time(17, 0)
# Where the after-hours line's night ends.
DAWN = time(5, 0)
FRIDAY = 4
# What staff may set the office to (the `office` key of config_changes in app/db/models.py): its
# hours decide, or it is forced open or closed.
SCHEDULE = "schedule"
FORCED_OPEN = "open"
FORCED_CLOSED = "closed"
OFFICE_OVERRIDES = (SCHEDULE, FORCED_OPEN, FORCED_CLOSED)


class ClockSettings(Protocol):
    """The part of the settings the clock reads. app.config.Settings has this shape."""

    @property
    def simulated_now(self) -> datetime | None: ...


def is_open(at: datetime) -> bool:
    """Monday to Friday, 09:00 up to but not including 17:00, in clinic time."""
    if at.utcoffset() is None:
        raise ValueError("is_open needs a timezone-aware datetime")
    local = at.astimezone(CLINIC_TZ)
    return local.weekday() <= FRIDAY and OPENS <= local.time() < CLOSES


def office_open(at: datetime, override: str) -> bool:
    """Whether the office is open for a call starting at `at`: as its hours say (`is_open`)
    with the override `schedule`, and as the override says when forced. Anything else is
    refused."""
    if override == SCHEDULE:
        return is_open(at)
    if override == FORCED_OPEN:
        return True
    if override == FORCED_CLOSED:
        return False
    raise ValueError("the office override is not one of its values")


def part_of_day(at: datetime) -> str:
    """`day` from 05:00 up to 17:00 in clinic time, `night` otherwise, for the after-hours
    line's good-bye."""
    if at.utcoffset() is None:
        raise ValueError("part_of_day needs a timezone-aware datetime")
    return "day" if DAWN <= at.astimezone(CLINIC_TZ).time() < CLOSES else "night"


def clinic_now(settings: ClockSettings) -> datetime:
    """The current clinic time, or the configured simulated time in clinic time."""
    simulated = settings.simulated_now
    if simulated is None:
        return datetime.now(CLINIC_TZ)
    if simulated.utcoffset() is None:
        raise ValueError("simulated_now needs a timezone")
    return simulated.astimezone(CLINIC_TZ)
