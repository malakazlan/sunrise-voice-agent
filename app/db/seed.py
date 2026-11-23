"""Load the clinic's synthetic seed data: `python -m app.db.seed`.

Insert-only: rows that are missing are added; rows that exist are never updated or deleted.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Appointment, AvailableSlot, Medication, Patient
from app.db.session import create_database_engine, create_session_factory
from app.domain.clinic import CLINIC_TZ, DR_LEE, DR_PATEL, clinic_date

logger = logging.getLogger(__name__)

SEED_FILE = Path(__file__).with_name("seed_data.json")

BUSINESS_DAYS_AHEAD = 14
GENERATED_TIMES = {
    DR_PATEL: (time(9, 30), time(11, 0), time(15, 0)),
    DR_LEE: (time(10, 0), time(13, 30)),
}
SCHEDULED = "scheduled"
# The same rules the database enforces, checked first so that a bad record is reported by
# field name only (hide_input_in_errors) and never reaches an INSERT.
LAST_FOUR = r"^[0-9]{4}$"

# For example "2026-12-03 10:30 AM with Dr. Patel", in clinic-local time.
APPOINTMENT_TEXT = re.compile(
    r"(?P<day>\d{4}-\d{2}-\d{2}) (?P<clock>\d{1,2}:\d{2} [AP]M) with (?P<doctor>.+)"
)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class MedicationRecord(_Record):
    name: str
    refills_remaining: Annotated[int, Field(ge=0)]
    last_filled: date
    pharmacy: str


class PatientRecord(_Record):
    id: str
    first_name: str
    last_name: str
    dob: date
    ssn_last4: Annotated[str, Field(pattern=LAST_FOUR)] | None
    phone: str
    medications: list[MedicationRecord]
    next_appointment: str | None
    lab_results_status: str
    balance_due: Annotated[Decimal, Field(ge=0)]

    @model_validator(mode="before")
    @classmethod
    def drop_full_ssn(cls, data: Any) -> Any:
        """A full SSN is never stored: an `ssn` field in the input is discarded unread."""
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if key != "ssn"}
        return data


class SlotDayRecord(_Record):
    doctor: str
    day: date = Field(alias="date")
    times: list[str]


class SeedData(_Record):
    patients: list[PatientRecord]
    available_slots: list[SlotDayRecord]


def load_seed_data(path: Path = SEED_FILE) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    return data


def _at_clinic(day: date, at: time) -> datetime:
    """A clinic-local wall-clock time on `day`; the offset follows daylight saving."""
    return datetime.combine(day, at, tzinfo=CLINIC_TZ)


def _clock(text: str) -> time:
    return datetime.strptime(text, "%I:%M %p").time()


def _appointment_row(patient: PatientRecord, text: str) -> dict[str, Any]:
    match = APPOINTMENT_TEXT.fullmatch(text)
    if match is None:
        raise ValueError(
            f"next_appointment of {patient.id} is not 'YYYY-MM-DD H:MM AM with <doctor>'"
        )
    return {
        "patient_id": patient.id,
        "doctor": match["doctor"],
        "starts_at": _at_clinic(date.fromisoformat(match["day"]), _clock(match["clock"])),
        "status": SCHEDULED,
    }


def _next_business_days(today: date) -> list[date]:
    """The next BUSINESS_DAYS_AHEAD Monday-to-Friday dates strictly after `today`."""
    days: list[date] = []
    day = today
    while len(days) < BUSINESS_DAYS_AHEAD:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    return days


async def _insert_missing_slots(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """Insert slots whose doctor and time are free; existing slots stay as they are."""
    if not rows:
        return 0
    statement = (
        insert(AvailableSlot)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["doctor", "starts_at"])
        .returning(AvailableSlot.id)
    )
    return len((await session.scalars(statement)).all())


async def ensure_generated_slots(session: AsyncSession, today: date) -> int:
    """Make sure the standard slots exist for the next business days. Returns rows added."""
    rows = [
        {"doctor": doctor, "starts_at": _at_clinic(day, at), "source": "generated"}
        for day in _next_business_days(today)
        for doctor, times in GENERATED_TIMES.items()
        for at in times
    ]
    return await _insert_missing_slots(session, rows)


async def seed(session: AsyncSession, raw: dict[str, Any], today: date) -> None:
    """Add the seed rows that are missing. The caller commits."""
    data = SeedData.model_validate(raw)

    patient_rows = [
        patient.model_dump(exclude={"medications", "next_appointment"}) for patient in data.patients
    ]
    inserted_ids = set(
        await session.scalars(
            insert(Patient)
            .values(patient_rows)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(Patient.id)
        )
    )

    # A patient that already existed keeps the medications and appointment it has now.
    new_patients = [patient for patient in data.patients if patient.id in inserted_ids]
    medication_rows = [
        {"patient_id": patient.id, **medication.model_dump()}
        for patient in new_patients
        for medication in patient.medications
    ]
    if medication_rows:
        await session.execute(insert(Medication).values(medication_rows))
    appointment_rows = [
        _appointment_row(patient, patient.next_appointment)
        for patient in new_patients
        if patient.next_appointment is not None
    ]
    if appointment_rows:
        await session.execute(insert(Appointment).values(appointment_rows))

    # Task slots go in first, so a generated slot never takes the place of a task slot.
    task_slots = await _insert_missing_slots(
        session,
        [
            {"doctor": day.doctor, "starts_at": _at_clinic(day.day, _clock(at)), "source": "task"}
            for day in data.available_slots
            for at in day.times
        ],
    )
    generated_slots = await ensure_generated_slots(session, today)

    logger.info(
        "seed_finished patients=%d medications=%d appointments=%d task_slots=%d generated_slots=%d",
        len(inserted_ids),
        len(medication_rows),
        len(appointment_rows),
        task_slots,
        generated_slots,
    )


async def _run(settings: Settings, today: date) -> None:
    engine = create_database_engine(settings)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            await seed(session, load_seed_data(), today)
    finally:
        await engine.dispose()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def main(clock: Callable[[], datetime] = _utc_now) -> None:
    """Seed the database from the environment's settings. The slot window starts from
    today's date at the clinic, taken from `clock` (the real clock unless a test passes one).
    """
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s [%(name)s] %(message)s")
    today = clinic_date(clock())
    # psycopg's async mode needs a selector event loop; Windows defaults to the proactor loop.
    asyncio.run(_run(settings, today), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
