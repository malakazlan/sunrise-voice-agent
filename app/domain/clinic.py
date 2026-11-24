from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

CLINIC_TZ = ZoneInfo("America/New_York")

# The clinic's doctors: the only names a slot, an appointment or a tool argument may carry.
Doctor = Literal["Dr. Patel", "Dr. Lee"]
DR_PATEL: Doctor = "Dr. Patel"
DR_LEE: Doctor = "Dr. Lee"
DOCTORS: tuple[Doctor, ...] = (DR_PATEL, DR_LEE)


def clinic_date(instant: datetime) -> date:
    """The calendar date at the clinic at `instant`, whatever the server's own time zone."""
    if instant.utcoffset() is None:
        raise ValueError("clinic_date needs a timezone-aware datetime")
    return instant.astimezone(CLINIC_TZ).date()
