from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from psycopg import errors
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DataError, IntegrityError

INSERT_PATIENT = text(
    "INSERT INTO patients (id, first_name, last_name, dob, ssn_last4, phone,"
    " lab_results_status, balance_due)"
    " VALUES (:id, 'Test', 'Patient', '1980-01-01', :ssn_last4, '555-000-0000',"
    " 'Pending', :balance_due)"
)
INSERT_MEDICATION = text(
    "INSERT INTO medications (patient_id, name, refills_remaining, last_filled, pharmacy)"
    " VALUES (:patient_id, :name, :refills_remaining, '2026-10-01', 'Test Pharmacy')"
)
INSERT_SLOT = text(
    "INSERT INTO available_slots (doctor, starts_at, source) VALUES (:doctor, :starts_at, 'task')"
)
INSERT_APPOINTMENT = text(
    "INSERT INTO appointments (patient_id, doctor, starts_at, status)"
    " VALUES (:patient_id, :doctor, :starts_at, 'scheduled')"
)

DEC_1_930_UTC = datetime(2026, 12, 1, 14, 30, tzinfo=UTC)


def execute(engine: Engine, statement: Any, **params: Any) -> None:
    with engine.begin() as connection:
        connection.execute(statement, params)


def insert_patient(
    engine: Engine, patient_id: str = "T001", ssn_last4: str | None = "1234", balance: str = "0"
) -> None:
    execute(engine, INSERT_PATIENT, id=patient_id, ssn_last4=ssn_last4, balance_due=balance)


def rejected_by(engine: Engine, statement: Any, **params: Any) -> errors.Error:
    """Run a statement that the database must refuse and return the driver's error."""
    with pytest.raises((IntegrityError, DataError)) as excinfo:
        execute(engine, statement, **params)
    original = excinfo.value.orig
    assert isinstance(original, errors.Error)
    return original


def check_violation(error: errors.Error) -> str | None:
    assert isinstance(error, errors.CheckViolation), type(error)
    return error.diag.constraint_name


def unique_violation(error: errors.Error) -> str | None:
    assert isinstance(error, errors.UniqueViolation), type(error)
    return error.diag.constraint_name


def test_ssn_last4_must_be_four_digits(owner_engine: Engine) -> None:
    for value in ("123", "12a4", "abcd", "1 34", "", "    "):
        error = rejected_by(
            owner_engine, INSERT_PATIENT, id="T001", ssn_last4=value, balance_due="0"
        )
        assert check_violation(error) == "ck_patients_ssn_last4_four_digits", value

    too_long = rejected_by(
        owner_engine, INSERT_PATIENT, id="T001", ssn_last4="12345", balance_due="0"
    )
    assert isinstance(too_long, errors.StringDataRightTruncation)

    insert_patient(owner_engine, "T001", "0042")
    insert_patient(owner_engine, "T002", None)
    with owner_engine.connect() as connection:
        stored = connection.execute(text("SELECT id, ssn_last4 FROM patients ORDER BY id")).all()
    assert [tuple(row) for row in stored] == [("T001", "0042"), ("T002", None)]


def test_no_column_can_hold_a_full_ssn(owner_engine: Engine) -> None:
    inspector = inspect(owner_engine)
    ssn_columns = [
        (table, column["name"], str(column["type"]))
        for table in inspector.get_table_names()
        for column in inspector.get_columns(table)
        if "ssn" in column["name"]
    ]
    assert ssn_columns == [("patients", "ssn_last4", "CHAR(4)")]


def test_duplicate_slot_is_rejected_by_database(owner_engine: Engine) -> None:
    execute(owner_engine, INSERT_SLOT, doctor="Dr. Patel", starts_at=DEC_1_930_UTC)
    execute(owner_engine, INSERT_SLOT, doctor="Dr. Lee", starts_at=DEC_1_930_UTC)

    same = rejected_by(owner_engine, INSERT_SLOT, doctor="Dr. Patel", starts_at=DEC_1_930_UTC)
    assert unique_violation(same) == "uq_available_slots_doctor_starts_at"

    # The same instant written as clinic-local time is still the same slot.
    local = datetime(2026, 12, 1, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    same_instant = rejected_by(owner_engine, INSERT_SLOT, doctor="Dr. Patel", starts_at=local)
    assert unique_violation(same_instant) == "uq_available_slots_doctor_starts_at"


def test_duplicate_appointment_time_is_rejected_by_database(owner_engine: Engine) -> None:
    insert_patient(owner_engine, "T001")
    insert_patient(owner_engine, "T002")
    execute(
        owner_engine,
        INSERT_APPOINTMENT,
        patient_id="T001",
        doctor="Dr. Lee",
        starts_at=DEC_1_930_UTC,
    )

    error = rejected_by(
        owner_engine,
        INSERT_APPOINTMENT,
        patient_id="T002",
        doctor="Dr. Lee",
        starts_at=DEC_1_930_UTC,
    )
    assert unique_violation(error) == "uq_appointments_doctor_starts_at"


def test_unknown_doctor_is_rejected(owner_engine: Engine) -> None:
    insert_patient(owner_engine, "T001")

    slot = rejected_by(owner_engine, INSERT_SLOT, doctor="Dr. Smith", starts_at=DEC_1_930_UTC)
    assert check_violation(slot) == "ck_available_slots_doctor"

    appointment = rejected_by(
        owner_engine,
        INSERT_APPOINTMENT,
        patient_id="T001",
        doctor="Dr. Smith",
        starts_at=DEC_1_930_UTC,
    )
    assert check_violation(appointment) == "ck_appointments_doctor"


def test_slot_source_must_be_task_or_generated(owner_engine: Engine) -> None:
    statement = text(
        "INSERT INTO available_slots (doctor, starts_at, source) VALUES ('Dr. Lee', :at, :source)"
    )
    error = rejected_by(owner_engine, statement, at=DEC_1_930_UTC, source="manual")
    assert check_violation(error) == "ck_available_slots_source"


def test_negative_amounts_are_rejected(owner_engine: Engine) -> None:
    balance = rejected_by(
        owner_engine, INSERT_PATIENT, id="T001", ssn_last4=None, balance_due="-0.01"
    )
    assert check_violation(balance) == "ck_patients_balance_due_not_negative"

    insert_patient(owner_engine, "T001", balance="0.00")
    refills = rejected_by(
        owner_engine, INSERT_MEDICATION, patient_id="T001", name="Test 1mg", refills_remaining=-1
    )
    assert check_violation(refills) == "ck_medications_refills_remaining_not_negative"

    execute(
        owner_engine, INSERT_MEDICATION, patient_id="T001", name="Test 1mg", refills_remaining=0
    )
