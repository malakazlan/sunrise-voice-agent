"""The only place engines are built, so every engine gets the same safety options."""

from typing import Any

from psycopg import Error as DriverError
from psycopg.pq import DiagnosticField
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.exc import DBAPIError, StatementError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings

# Statement parameters are row values (names, last four digits). With this option an error
# shows the SQL but not the values bound to it.
SAFE_ENGINE_OPTIONS: dict[str, Any] = {"hide_parameters": True}

# Diagnostic fields that only name things in the schema, so they cannot hold a row value.
# Everything else the server sends (its message, DETAIL with the failing row, HINT, CONTEXT,
# the internal query) is dropped.
KEPT_DIAGNOSTICS = (
    DiagnosticField.SEVERITY_NONLOCALIZED,
    DiagnosticField.SQLSTATE,
    DiagnosticField.SCHEMA_NAME,
    DiagnosticField.TABLE_NAME,
    DiagnosticField.COLUMN_NAME,
    DiagnosticField.DATATYPE_NAME,
    DiagnosticField.CONSTRAINT_NAME,
)


def _summary(error: DriverError) -> str:
    diag = error.diag
    parts = [f"{type(error).__name__} (SQLSTATE {error.sqlstate})"]
    for label, name in (
        ("constraint", diag.constraint_name),
        ("table", diag.table_name),
        ("column", diag.column_name),
    ):
        if name:
            parts.append(f"{label} {name}")
    return ", ".join(parts) + "; server message and row values withheld"


def withhold_row_values(context: ExceptionContext) -> StatementError | None:
    """Make sure no error leaving the engine carries row values.

    SQLAlchemy raises the exception a handler returns `from` the original error, so the
    original is always scrubbed in place as well. Exceptions that are not `Exception`
    (interrupts, cancellation) are left alone.
    """
    original = context.original_exception
    wrapped = context.sqlalchemy_exception
    if isinstance(original, DriverError):
        return _withhold_driver_error(original, wrapped)
    if isinstance(original, Exception):
        return _withhold_other_error(original, wrapped)
    return None


def _withhold_other_error(
    original: Exception, wrapped: StatementError | None
) -> StatementError | None:
    """An error raised inside the engine by something other than the driver, for example a
    value refused by a validating type. Its message may quote the value, so only its class
    is kept, and whatever was chained to it is cut off.
    """
    original.args = (f"{type(original).__name__}: message withheld",)
    original.__cause__ = None
    original.__context__ = None
    original.__suppress_context__ = True
    if type(wrapped) is not StatementError:
        return None
    original_class = f"{type(original).__module__}.{type(original).__qualname__}"
    return StatementError(
        f"({original_class}) message withheld",
        wrapped.statement,
        None,
        original,
        hide_parameters=True,
        ismulti=wrapped.ismulti,
    )


def _withhold_driver_error(
    original: DriverError, wrapped: StatementError | None
) -> StatementError | None:
    """A driver error: its message becomes the summary, and its diagnostics keep only
    KEPT_DIAGNOSTICS. psycopg accepts a dict for this (it is the form it pickles). The
    SQLAlchemy exception class, the driver's exception class, the SQLSTATE and the
    constraint name all stay as they were.
    """
    if original.pgresult is None and original.sqlstate is None:
        # Client side, for example a lost connection: the driver's own words, which carry
        # no row values and are needed as they are.
        return None

    diag = original.diag
    kept: dict[int, bytes | None] = {}
    for field in KEPT_DIAGNOSTICS:
        value = getattr(diag, field.name.lower())
        if value is not None:
            kept[field] = value.encode()
    summary = _summary(original)
    kept[DiagnosticField.MESSAGE_PRIMARY] = summary.encode()

    original.args = (summary,)
    original._info = kept
    original._encoding = "utf-8"
    original._pgconn = None

    if not isinstance(wrapped, DBAPIError):
        return None
    return type(wrapped)(
        wrapped.statement,
        None,
        original,
        hide_parameters=True,
        connection_invalidated=wrapped.connection_invalidated,
        code=wrapped.code,
        ismulti=wrapped.ismulti,
    )


def _protect(engine: Engine) -> None:
    event.listen(engine, "handle_error", withhold_row_values, retval=True)


def create_database_engine(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, **SAFE_ENGINE_OPTIONS)
    _protect(engine.sync_engine)
    return engine


def create_sync_engine(settings: Settings, **options: Any) -> Engine:
    """A synchronous engine, for Alembic and for test set-up."""
    engine = create_engine(settings.database_url, **options, **SAFE_ENGINE_OPTIONS)
    _protect(engine)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
