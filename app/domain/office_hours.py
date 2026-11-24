from datetime import datetime, time
from typing import Protocol

from app.domain.clinic import CLINIC_TZ

OPENS = time(9, 0)
CLOSES = time(17, 0)
FRIDAY = 4


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


def clinic_now(settings: ClockSettings) -> datetime:
    """The current clinic time, or the configured simulated time in clinic time."""
    simulated = settings.simulated_now
    if simulated is None:
        return datetime.now(CLINIC_TZ)
    if simulated.utcoffset() is None:
        raise ValueError("simulated_now needs a timezone")
    return simulated.astimezone(CLINIC_TZ)
