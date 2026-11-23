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
    if at.tzinfo is None:
        raise ValueError("is_open needs a timezone-aware datetime")
    local = at.astimezone(CLINIC_TZ)
    return local.weekday() <= FRIDAY and OPENS <= local.time() < CLOSES


def clinic_now(settings: ClockSettings) -> datetime:
    """The current time, or the configured simulated time when there is one."""
    if settings.simulated_now is not None:
        return settings.simulated_now
    return datetime.now(CLINIC_TZ)
