from datetime import date, datetime
from zoneinfo import ZoneInfo

CLINIC_TZ = ZoneInfo("America/New_York")

DR_PATEL = "Dr. Patel"
DR_LEE = "Dr. Lee"
DOCTORS = (DR_PATEL, DR_LEE)


def clinic_date(instant: datetime) -> date:
    """The calendar date at the clinic at `instant`, whatever the server's own time zone."""
    if instant.tzinfo is None:
        raise ValueError("clinic_date needs a timezone-aware datetime")
    return instant.astimezone(CLINIC_TZ).date()
