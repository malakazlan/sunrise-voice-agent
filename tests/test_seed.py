import logging
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import pytest
from sqlalchemy import Text, column, select, table, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.seed import ensure_generated_slots, load_seed_data, seed
from app.db.session import create_database_engine, create_session_factory

pytestmark = pytest.mark.anyio

NEW_YORK = ZoneInfo("America/New_York")
TUESDAY = date(2026, 11, 24)
NEXT_14_BUSINESS_DAYS_AFTER_TUESDAY = [
    date(2026, 11, 25),
    date(2026, 11, 26),
    date(2026, 11, 27),
    date(2026, 11, 30),
    date(2026, 12, 1),
    date(2026, 12, 2),
    date(2026, 12, 3),
    date(2026, 12, 4),
    date(2026, 12, 7),
    date(2026, 12, 8),
    date(2026, 12, 9),
    date(2026, 12, 10),
    date(2026, 12, 11),
    date(2026, 12, 14),
]
AFTER_SEEDING = {"patients": 5, "medications": 5, "appointments": 2, "available_slots": 72}

COUNTS = text(
    "SELECT (SELECT count(*) FROM patients), (SELECT count(*) FROM medications),"
    " (SELECT count(*) FROM appointments), (SELECT count(*) FROM available_slots)"
)
SNAPSHOT = (
    text("SELECT * FROM patients ORDER BY id"),
    text("SELECT * FROM medications ORDER BY id"),
    text("SELECT * FROM appointments ORDER BY id"),
    text("SELECT * FROM available_slots ORDER BY id"),
)
SLOTS = text("SELECT doctor, starts_at, source, is_available FROM available_slots")
APPOINTMENTS = text(
    "SELECT patient_id, doctor, starts_at, slot_id, status FROM appointments ORDER BY patient_id"
)
TEXT_COLUMNS = text(
    "SELECT table_name, column_name FROM information_schema.columns"
    " WHERE table_schema = 'public'"
    " AND data_type IN ('text', 'character', 'character varying')"
)


async def run_seed(
    session: AsyncSession, data: dict[str, Any] | None = None, today: date = TUESDAY
) -> None:
    await seed(session, load_seed_data() if data is None else data, today)
    await session.commit()


async def counts(session: AsyncSession) -> dict[str, int]:
    row = (await session.execute(COUNTS)).one()
    return dict(zip(AFTER_SEEDING, row, strict=True))


async def snapshot(session: AsyncSession) -> list[list[tuple[Any, ...]]]:
    return [[tuple(row) for row in (await session.execute(query)).all()] for query in SNAPSHOT]


async def slots(session: AsyncSession) -> list[tuple[str, datetime, str, bool]]:
    return [
        (doctor, starts_at, source, is_available)
        for doctor, starts_at, source, is_available in (await session.execute(SLOTS)).all()
    ]


def clinic_day(moment: datetime) -> date:
    return moment.astimezone(NEW_YORK).date()


def clinic_clock(moment: datetime) -> time:
    return moment.astimezone(NEW_YORK).time()


def test_seed_file_carries_only_last_four() -> None:
    data = load_seed_data()

    assert [patient["id"] for patient in data["patients"]] == [
        "P001",
        "P002",
        "P003",
        "P004",
        "P005",
    ]
    for patient in data["patients"]:
        assert "ssn" not in patient, patient["id"]
        assert re.fullmatch(r"[0-9]{4}", patient["ssn_last4"]), patient["id"]
    assert sum(len(day["times"]) for day in data["available_slots"]) == 7


async def test_first_seed_inserts_the_task_data(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    await run_seed(session)

    assert await counts(session) == AFTER_SEEDING

    patients = (
        await session.execute(
            text(
                "SELECT id, first_name, last_name, dob, ssn_last4, phone, lab_results_status,"
                " balance_due, self_pay, is_new, possible_duplicate FROM patients"
                " WHERE id IN ('P001', 'P004', 'P005') ORDER BY id"
            )
        )
    ).all()
    assert [tuple(row) for row in patients] == [
        (
            "P001",
            "Sarah",
            "Johnson",
            date(1985, 3, 14),
            "3344",
            "555-201-3344",
            "Ready - doctor will call to discuss",
            Decimal("45.00"),
            False,
            False,
            False,
        ),
        (
            "P004",
            "James",
            "Carter",
            date(1968, 1, 30),
            "7712",
            "555-310-7712",
            "Pending",
            Decimal("0.00"),
            False,
            False,
            False,
        ),
        (
            "P005",
            "Emily",
            "Nguyen",
            date(2001, 9, 5),
            "4406",
            "555-672-4406",
            "No recent labs",
            Decimal("15.25"),
            False,
            False,
            False,
        ),
    ]

    medications = (
        await session.execute(
            text(
                "SELECT patient_id, name, refills_remaining, last_filled, pharmacy"
                " FROM medications ORDER BY patient_id, name"
            )
        )
    ).all()
    assert [tuple(row) for row in medications] == [
        ("P001", "Lisinopril 10mg", 2, date(2026, 10, 20), "CVS Main St"),
        ("P001", "Metformin 500mg", 0, date(2026, 10, 2), "CVS Main St"),
        ("P002", "Atorvastatin 20mg", 1, date(2026, 9, 15), "Walgreens Oak Ave"),
        ("P003", "Levothyroxine 50mcg", 3, date(2026, 11, 1), "Rite Aid 5th St"),
        ("P005", "Albuterol inhaler", 0, date(2026, 8, 11), "CVS Main St"),
    ]

    appointments = (await session.execute(APPOINTMENTS)).all()
    assert [tuple(row) for row in appointments] == [
        ("P001", "Dr. Patel", datetime(2026, 12, 3, 15, 30, tzinfo=UTC), None, "scheduled"),
        ("P003", "Dr. Lee", datetime(2026, 11, 28, 19, 0, tzinfo=UTC), None, "scheduled"),
    ]

    task_slots = sorted(
        (doctor, starts_at, is_available)
        for doctor, starts_at, source, is_available in await slots(session)
        if source == "task"
    )
    assert task_slots == [
        ("Dr. Lee", datetime(2026, 12, 2, 15, 0, tzinfo=UTC), True),
        ("Dr. Lee", datetime(2026, 12, 2, 18, 30, tzinfo=UTC), True),
        ("Dr. Patel", datetime(2026, 12, 1, 14, 30, tzinfo=UTC), True),
        ("Dr. Patel", datetime(2026, 12, 1, 16, 0, tzinfo=UTC), True),
        ("Dr. Patel", datetime(2026, 12, 1, 20, 0, tzinfo=UTC), True),
        ("Dr. Patel", datetime(2026, 12, 4, 14, 0, tzinfo=UTC), True),
        ("Dr. Patel", datetime(2026, 12, 4, 19, 30, tzinfo=UTC), True),
    ]

    # The seed logs counts, never names or numbers from the records.
    assert caplog.records
    for value in ("Sarah", "Johnson", "3344", "555-201-3344", "Lisinopril"):
        assert value not in caplog.text


async def test_full_ssn_in_input_is_never_stored(session: AsyncSession) -> None:
    data = load_seed_data()
    full_numbers = []
    for index, patient in enumerate(data["patients"]):
        full_number = "-".join([str(index + 1) * 3, "4" * 2, patient["ssn_last4"]])
        patient["ssn"] = full_number
        full_numbers.append(full_number)

    await run_seed(session, data)

    assert await counts(session) == AFTER_SEEDING
    ssn_shaped = re.compile(r"(?<!\d)\d{3}-?\d{2}-?\d{4}(?!\d)")
    columns = (await session.execute(TEXT_COLUMNS)).all()
    assert len(columns) > 10
    for table_name, column_name in columns:
        query = select(column(column_name, Text)).select_from(table(table_name))
        values = (await session.scalars(query)).all()
        for value in values:
            if value is None:
                continue
            assert not ssn_shaped.search(value), (table_name, column_name)
            for full_number in full_numbers:
                assert full_number.replace("-", "") not in value.replace("-", "")
    stored_last_four = (
        await session.scalars(text("SELECT ssn_last4 FROM patients ORDER BY id"))
    ).all()
    assert stored_last_four == ["3344", "1020", "9081", "7712", "4406"]


async def test_reseed_changes_nothing(session: AsyncSession) -> None:
    await run_seed(session)
    before = await snapshot(session)

    await run_seed(session)

    assert await snapshot(session) == before
    assert await counts(session) == AFTER_SEEDING


async def test_reseed_keeps_booked_slot_unavailable(session: AsyncSession) -> None:
    await run_seed(session)
    task_slot = datetime(2026, 12, 1, 14, 30, tzinfo=UTC)  # Dr. Patel, 9:30 AM, from the task
    generated_slot = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # Dr. Lee, 10:00 AM, generated
    await session.execute(
        text(
            "UPDATE available_slots SET is_available = false"
            " WHERE (doctor = 'Dr. Patel' AND starts_at = :task)"
            " OR (doctor = 'Dr. Lee' AND starts_at = :generated)"
        ),
        {"task": task_slot, "generated": generated_slot},
    )
    await session.commit()

    await run_seed(session)

    booked = sorted(
        (doctor, starts_at, source)
        for doctor, starts_at, source, is_available in await slots(session)
        if not is_available
    )
    assert booked == [
        ("Dr. Lee", generated_slot, "generated"),
        ("Dr. Patel", task_slot, "task"),
    ]
    assert await counts(session) == AFTER_SEEDING


async def test_reseed_does_not_overwrite_existing_rows(session: AsyncSession) -> None:
    await run_seed(session)
    moved_to = datetime(2026, 12, 4, 16, 0, tzinfo=UTC)
    await session.execute(
        text(
            "UPDATE patients SET phone = '555-999-0000', balance_due = 0,"
            " lab_results_status = 'Discussed' WHERE id = 'P001'"
        )
    )
    await session.execute(
        text(
            "UPDATE medications SET refills_remaining = 3"
            " WHERE patient_id = 'P001' AND name = 'Metformin 500mg'"
        )
    )
    await session.execute(
        text(
            "UPDATE appointments SET starts_at = :moved_to, status = 'rescheduled'"
            " WHERE patient_id = 'P001'"
        ),
        {"moved_to": moved_to},
    )
    await session.commit()

    await run_seed(session)

    patient = (
        await session.execute(
            text("SELECT phone, balance_due, lab_results_status FROM patients WHERE id = 'P001'")
        )
    ).one()
    assert tuple(patient) == ("555-999-0000", 0, "Discussed")
    refills = await session.scalar(
        text("SELECT refills_remaining FROM medications WHERE name = 'Metformin 500mg'")
    )
    assert refills == 3
    appointments = (await session.execute(APPOINTMENTS)).all()
    assert [tuple(row) for row in appointments] == [
        ("P001", "Dr. Patel", moved_to, None, "rescheduled"),
        ("P003", "Dr. Lee", datetime(2026, 11, 28, 19, 0, tzinfo=UTC), None, "scheduled"),
    ]
    assert await counts(session) == AFTER_SEEDING


async def test_generated_slots_cover_next_14_business_days(session: AsyncSession) -> None:
    await run_seed(session, today=TUESDAY)

    all_slots = await slots(session)
    generated = [(doctor, at) for doctor, at, source, _ in all_slots if source == "generated"]
    assert sorted({clinic_day(at) for _, at in generated}) == NEXT_14_BUSINESS_DAYS_AFTER_TUESDAY
    assert len(generated) == 65
    assert len(all_slots) == 72
    assert sum(1 for *_, source, _ in all_slots if source == "task") == 7

    def generated_on(day: date) -> list[tuple[str, time]]:
        return sorted(
            (doctor, clinic_clock(at)) for doctor, at in generated if clinic_day(at) == day
        )

    assert generated_on(date(2026, 11, 25)) == [
        ("Dr. Lee", time(10, 0)),
        ("Dr. Lee", time(13, 30)),
        ("Dr. Patel", time(9, 30)),
        ("Dr. Patel", time(11, 0)),
        ("Dr. Patel", time(15, 0)),
    ]
    # Dr. Patel's three times on Dec 1 and Dr. Lee's two on Dec 2 are task slots already.
    assert generated_on(date(2026, 12, 1)) == [("Dr. Lee", time(10, 0)), ("Dr. Lee", time(13, 30))]
    assert generated_on(date(2026, 12, 2)) == [
        ("Dr. Patel", time(9, 30)),
        ("Dr. Patel", time(11, 0)),
        ("Dr. Patel", time(15, 0)),
    ]


@pytest.mark.parametrize(
    "today",
    [date(2026, 11, 27), date(2026, 11, 28), date(2026, 11, 29)],
    ids=["friday", "saturday", "sunday"],
)
async def test_generated_slots_start_on_next_business_day(
    session: AsyncSession, today: date
) -> None:
    inserted = await ensure_generated_slots(session, today)
    await session.commit()

    days = sorted({clinic_day(at) for _, at, _, _ in await slots(session)})
    assert inserted == 70
    assert days[0] == date(2026, 11, 30)
    assert days[-1] == date(2026, 12, 17)
    assert len(days) == 14


async def test_generated_slots_cross_the_year_boundary(session: AsyncSession) -> None:
    inserted = await ensure_generated_slots(session, date(2026, 12, 30))
    await session.commit()

    days = sorted({clinic_day(at) for _, at, _, _ in await slots(session)})
    assert inserted == 70
    assert days == [
        date(2026, 12, 31),
        date(2027, 1, 1),
        date(2027, 1, 4),
        date(2027, 1, 5),
        date(2027, 1, 6),
        date(2027, 1, 7),
        date(2027, 1, 8),
        date(2027, 1, 11),
        date(2027, 1, 12),
        date(2027, 1, 13),
        date(2027, 1, 14),
        date(2027, 1, 15),
        date(2027, 1, 18),
        date(2027, 1, 19),
    ]


async def test_clinic_times_respect_dst(session: AsyncSession) -> None:
    # Seed in summer, with one appointment moved into summer time as well.
    data = load_seed_data()
    data["patients"][1]["next_appointment"] = "2026-07-01 9:30 AM with Dr. Patel"

    await run_seed(session, data, today=date(2026, 6, 30))

    patel = {
        (at, source) for doctor, at, source, _ in await slots(session) if doctor == "Dr. Patel"
    }
    assert (datetime(2026, 12, 1, 14, 30, tzinfo=UTC), "task") in patel
    assert (datetime(2026, 7, 1, 13, 30, tzinfo=UTC), "generated") in patel
    assert (datetime(2026, 7, 1, 14, 30, tzinfo=UTC), "generated") not in patel

    appointments = {
        patient_id: starts_at
        for patient_id, _, starts_at, _, _ in (await session.execute(APPOINTMENTS)).all()
    }
    assert appointments == {
        "P001": datetime(2026, 12, 3, 15, 30, tzinfo=UTC),
        "P002": datetime(2026, 7, 1, 13, 30, tzinfo=UTC),
        "P003": datetime(2026, 11, 28, 19, 0, tzinfo=UTC),
    }


async def test_concurrent_seeds_do_not_duplicate_rows(database: Settings) -> None:
    engine = create_database_engine(database)
    sessions = create_session_factory(engine)

    async def seed_once() -> None:
        async with sessions() as session:
            await run_seed(session)

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(seed_once)
            group.start_soon(seed_once)
        async with sessions() as session:
            assert await counts(session) == AFTER_SEEDING
    finally:
        await engine.dispose()


def test_seed_module_runs_twice_against_database(
    database: Settings, run_module: Callable[..., subprocess.CompletedProcess[str]]
) -> None:
    first = run_module(database, "app.db.seed")
    assert first.returncode == 0, first.stderr
    second = run_module(database, "app.db.seed")
    assert second.returncode == 0, second.stderr
    assert "patients=5" in first.stderr
    assert "patients=0" in second.stderr


async def test_unreadable_appointment_text_is_rejected(session: AsyncSession) -> None:
    data = load_seed_data()
    data["patients"][1]["next_appointment"] = "next Tuesday at ten with Dr. Lee"

    with pytest.raises(ValueError, match="next_appointment of P002") as excinfo:
        await run_seed(session, data)

    assert "Tuesday" not in str(excinfo.value)
