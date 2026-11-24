import traceback
from collections.abc import Awaitable, Callable, Iterator
from datetime import date
from typing import Any

import pytest
from psycopg import errors
from sqlalchemy import (
    Connection,
    Dialect,
    Text,
    TextClause,
    TypeDecorator,
    event,
    literal,
    select,
    text,
)
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, StatementError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db.session import create_database_engine, create_sync_engine, withhold_row_values

pytestmark = pytest.mark.anyio

OFFLINE = Settings(
    _env_file=None,
    app_env="dev",
    postgres_host="db.invalid",
    postgres_db="sunrise_offline",
    postgres_user="sunrise_offline",
    postgres_password="offline-test-password",
    audit_hmac_key="offline-audit-key",
)

# A synthetic patient with values that occur nowhere else, so any copy of them in an error
# is a leak. They are always sent as bound parameters, never written into the SQL text.
ROW = {
    "id": "P900",
    "first_name": "Zebulon",
    "last_name": "Quackenbush",
    "dob": date(1933, 3, 3),
    "ssn_last4": "8642",
    "phone": "555-019-7531",
    "lab_results_status": "Pending",
    "balance_due": 0,
}
ROW_VALUES = ("P900", "Zebulon", "Quackenbush", "1933-03-03", "8642", "555-019-7531")
MEDICATION = "Zanzibarol 7mg"

INSERT_PATIENT = text(
    "INSERT INTO patients (id, first_name, last_name, dob, ssn_last4, phone,"
    " lab_results_status, balance_due)"
    " VALUES (:id, :first_name, :last_name, :dob, :ssn_last4, :phone,"
    " :lab_results_status, :balance_due)"
)
INSERT_MEDICATION = text(
    "INSERT INTO medications (patient_id, name, refills_remaining, last_filled, pharmacy)"
    " VALUES (:patient_id, :name, 1, :last_filled, :pharmacy)"
)
MEDICATION_ROW = {
    "patient_id": "P900",
    "name": MEDICATION,
    "last_filled": date(1999, 9, 9),
    "pharmacy": "Quackenbush Pharmacy",
}

# Each violation: rows inserted first, then the statement the database refuses.
VIOLATIONS: dict[str, tuple[list[tuple[TextClause, dict[str, Any]]], TextClause, dict[str, Any]]]
VIOLATIONS = {
    "check": ([], INSERT_PATIENT, {**ROW, "balance_due": -5}),
    "not_null": ([], INSERT_PATIENT, {**ROW, "last_name": None}),
    "unique": (
        [(INSERT_PATIENT, ROW), (INSERT_MEDICATION, MEDICATION_ROW)],
        INSERT_MEDICATION,
        MEDICATION_ROW,
    ),
}
SECRET_VALUES = (*ROW_VALUES, MEDICATION, "1999-09-09", "Quackenbush Pharmacy")


def raised_by_sync(settings: Settings, violation: str) -> DBAPIError:
    setup, statement, params = VIOLATIONS[violation]
    engine = create_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            for setup_statement, setup_params in setup:
                connection.execute(setup_statement, setup_params)
        with pytest.raises(DBAPIError) as excinfo, engine.begin() as connection:
            connection.execute(statement, params)
    finally:
        engine.dispose()
    return excinfo.value


async def raised_by_async(settings: Settings, violation: str) -> DBAPIError:
    setup, statement, params = VIOLATIONS[violation]
    engine = create_database_engine(settings)
    try:
        async with engine.begin() as connection:
            for setup_statement, setup_params in setup:
                await connection.execute(setup_statement, setup_params)
        with pytest.raises(DBAPIError) as excinfo:
            async with engine.begin() as connection:
                await connection.execute(statement, params)
    finally:
        await engine.dispose()
    return excinfo.value


async def raised(settings: Settings, violation: str, kind: str) -> DBAPIError:
    if kind == "sync":
        return raised_by_sync(settings, violation)
    return await raised_by_async(settings, violation)


def chain(error: BaseException) -> Iterator[BaseException]:
    """The error and everything chained to it, including SQLAlchemy's `orig`."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or any(current is known for known in seen):
            continue
        seen.append(current)
        pending += [current.__cause__, current.__context__, getattr(current, "orig", None)]
    return iter(seen)


def assert_carries_no_row_values(error: DBAPIError) -> None:
    rendered = [str(error), repr(error), "".join(traceback.format_exception(error))]
    driver_errors = []
    for linked in chain(error):
        rendered += [str(linked), repr(linked), repr(getattr(linked, "params", None))]
        if isinstance(linked, errors.Error):
            driver_errors.append(linked)
            rendered += [
                repr(linked.diag.message_primary),
                repr(linked.diag.message_detail),
                repr(linked.diag.message_hint),
                repr(linked.diag.context),
                repr(linked.diag.internal_query),
            ]
    for value in SECRET_VALUES:
        for text_of_error in rendered:
            assert value not in text_of_error, value
    assert driver_errors, "the driver's error is still attached"
    for driver_error in driver_errors:
        assert driver_error.pgresult is None, "the driver's result object holds the failing row"
    # The real failure is still visible: it is a constraint error from the database.
    assert "SQLSTATE 23" in str(error)


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_check_violation_does_not_carry_row_values(database: Settings, kind: str) -> None:
    error = await raised(database, "check", kind)

    assert isinstance(error.orig, errors.CheckViolation)
    assert_carries_no_row_values(error)


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_not_null_violation_does_not_carry_row_values(database: Settings, kind: str) -> None:
    error = await raised(database, "not_null", kind)

    assert isinstance(error.orig, errors.NotNullViolation)
    assert_carries_no_row_values(error)


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_unique_violation_does_not_carry_row_values(database: Settings, kind: str) -> None:
    error = await raised(database, "unique", kind)

    assert isinstance(error.orig, errors.UniqueViolation)
    assert_carries_no_row_values(error)


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_failure_still_reports_class_sqlstate_and_constraint(
    database: Settings, kind: str
) -> None:
    expected = {
        "check": (errors.CheckViolation, "23514", "ck_patients_balance_due_not_negative", None),
        "not_null": (errors.NotNullViolation, "23502", None, "last_name"),
        "unique": (errors.UniqueViolation, "23505", "uq_medications_patient_id_name", None),
    }
    tables = {"check": "patients", "not_null": "patients", "unique": "medications"}
    for violation, (driver_class, sqlstate, constraint, column) in expected.items():
        error = await raised(database, violation, kind)

        assert isinstance(error, IntegrityError), violation
        assert type(error.orig) is driver_class, violation
        assert isinstance(error.orig, errors.Error)
        assert error.orig.sqlstate == sqlstate
        assert error.orig.diag.sqlstate == sqlstate
        assert error.orig.diag.constraint_name == constraint
        assert error.orig.diag.column_name == column
        assert error.orig.diag.table_name == tables[violation]
        assert f"SQLSTATE {sqlstate}" in str(error)
        if constraint is not None:
            assert constraint in str(error)


def test_every_engine_hides_parameters() -> None:
    assert create_database_engine(OFFLINE).sync_engine.hide_parameters is True
    assert create_sync_engine(OFFLINE).hide_parameters is True


def test_every_engine_withholds_row_values_from_errors() -> None:
    async_engine = create_database_engine(OFFLINE).sync_engine
    sync_engine = create_sync_engine(OFFLINE)

    assert event.contains(async_engine, "handle_error", withhold_row_values)
    assert event.contains(sync_engine, "handle_error", withhold_row_values)


class RefusesValues(TypeDecorator[str]):
    """A validating type: it refuses every value and quotes it, as careless code would."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str:
        raise ValueError(f"refusing {value!r}")


BOUND_VALUE = "Zebulon Quackenbush 1933-03-03"
REFUSED = select(literal(BOUND_VALUE, type_=RefusesValues()))


async def non_driver_error(settings: Settings, kind: str) -> StatementError:
    if kind == "sync":
        engine = create_sync_engine(settings, poolclass=NullPool)
        try:
            with pytest.raises(StatementError) as excinfo, engine.connect() as connection:
                connection.execute(REFUSED)
        finally:
            engine.dispose()
        return excinfo.value
    async_engine = create_database_engine(settings)
    try:
        with pytest.raises(StatementError) as excinfo:
            async with async_engine.connect() as connection:
                await connection.execute(REFUSED)
    finally:
        await async_engine.dispose()
    return excinfo.value


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_non_driver_error_does_not_carry_values(database: Settings, kind: str) -> None:
    error = await non_driver_error(database, kind)

    assert type(error) is StatementError
    assert type(error.orig) is ValueError
    rendered = [str(error), repr(error), "".join(traceback.format_exception(error))]
    rendered += [text_of for linked in chain(error) for text_of in (str(linked), repr(linked))]
    for text_of_error in rendered:
        assert "Zebulon" not in text_of_error
        assert "1933" not in text_of_error
    assert "ValueError" in str(error)


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_lost_connection_reason_passes_through(
    database: Settings,
    kind: str,
    close_driver_connection: Callable[[Connection], None],
    close_async_driver_connection: Callable[[AsyncConnection], Awaitable[None]],
) -> None:
    if kind == "sync":
        engine = create_sync_engine(database, poolclass=NullPool)
        try:
            with pytest.raises(OperationalError) as excinfo, engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                close_driver_connection(connection)
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    else:
        async_engine = create_database_engine(database)
        try:
            with pytest.raises(OperationalError) as excinfo:
                async with async_engine.connect() as async_connection:
                    await async_connection.execute(text("SELECT 1"))
                    await close_async_driver_connection(async_connection)
                    await async_connection.execute(text("SELECT 1"))
        finally:
            await async_engine.dispose()

    # A client-side failure: no server result, no SQLSTATE, the driver's own words.
    assert isinstance(excinfo.value.orig, errors.OperationalError)
    assert excinfo.value.orig.sqlstate is None
    assert "the connection is closed" in str(excinfo.value)
    assert "withheld" not in str(excinfo.value)


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_administrator_kill_reports_class_and_sqlstate(database: Settings, kind: str) -> None:
    admin = create_sync_engine(database, isolation_level="AUTOCOMMIT", poolclass=NullPool)

    def kill(pid: int) -> None:
        with admin.connect() as killer:
            killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

    try:
        if kind == "sync":
            engine = create_sync_engine(database, poolclass=NullPool)
            try:
                with pytest.raises(OperationalError) as excinfo, engine.connect() as connection:
                    kill(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
                    connection.execute(text("SELECT 1"))
            finally:
                engine.dispose()
        else:
            async_engine = create_database_engine(database)
            try:
                with pytest.raises(OperationalError) as excinfo:
                    async with async_engine.connect() as async_connection:
                        pid = await async_connection.execute(text("SELECT pg_backend_pid()"))
                        kill(pid.scalar_one())
                        await async_connection.execute(text("SELECT 1"))
            finally:
                await async_engine.dispose()
    finally:
        admin.dispose()

    assert type(excinfo.value.orig) is errors.AdminShutdown
    assert "AdminShutdown (SQLSTATE 57P01)" in str(excinfo.value)
