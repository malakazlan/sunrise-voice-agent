"""The only place engines are built, so every engine gets the same safety options."""

import re
from typing import Any

from psycopg import Error as DriverError
from psycopg import sql
from psycopg.errors import LockNotAvailable, QueryCanceled
from psycopg.pq import DiagnosticField, TransactionStatus
from pydantic import SecretStr
from sqlalchemy import URL, Connection, Engine, create_engine, event
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.exc import DBAPIError, StatementError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import DatabaseSettings, StartupSettings

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


# The server ends a session that sits this long inside a transaction without a statement, so
# an abandoned transaction cannot hold the audit chain's lock (or any other) indefinitely.
# Statements that run, however long, are not idle; migrations and the seed run statement after
# statement.
IDLE_IN_TRANSACTION_TIMEOUT_MS = 30_000


# Every session of the start-up steps (the owner's engines) waits at most this long for any
# one lock, and runs no statement longer than this, so a session of another role holding a lock
# they need (the runtime role can take the role set-up's advisory lock, or lock a table it may
# write) makes the step fail instead of waiting for ever. Measured on an empty database, the
# longest start-up statement takes well under a second; a migration that rewrites a large table
# would be the one to need more.
STARTUP_LOCK_TIMEOUT_MS = 30_000
STARTUP_STATEMENT_TIMEOUT_MS = 300_000


# Every engine gives up opening a connection to a server that does not answer after this long,
# instead of psycopg's default of 130 seconds: a tool call waiting on the database holds up the
# caller's conversation. libpq's `connect_timeout` (whole seconds); psycopg enforces it itself
# on its synchronous and asynchronous connections alike, per host it tries.
CONNECT_TIMEOUT_SECONDS = 5


# Every statement of the application's sessions (the runtime role) ends after this long, so a
# connected but stalled server cannot hold a tool call. A statement waiting for a lock is
# running too: the audit log's own lock wait (LOCK_TIMEOUT_MS in app/audit/log.py) is just as
# long, so whichever ends that wait first, the append fails the same way.
RUNTIME_STATEMENT_TIMEOUT_MS = 5_000


def _connect_args(startup: bool = False) -> dict[str, Any]:
    # libpq's `options` sets the server setting for the whole session, at connect time.
    options = f"-c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_TIMEOUT_MS}"
    if startup:
        options += (
            f" -c lock_timeout={STARTUP_LOCK_TIMEOUT_MS}"
            f" -c statement_timeout={STARTUP_STATEMENT_TIMEOUT_MS}"
        )
    else:
        options += f" -c statement_timeout={RUNTIME_STATEMENT_TIMEOUT_MS}"
    return {"options": options, "connect_timeout": CONNECT_TIMEOUT_SECONDS}


def startup_wait_failure(
    step: str, error: BaseException, waiting_for: str = "a lock on a database object"
) -> RuntimeError | None:
    """The failure a start-up step reports when its lock or statement timeout ended a
    statement, naming what it waited for; None for any other error."""
    original = getattr(error, "orig", error)
    if isinstance(original, LockNotAvailable):
        seconds = STARTUP_LOCK_TIMEOUT_MS // 1000
        return RuntimeError(
            f"{step}: waited more than {seconds} s for {waiting_for}, which another session"
            " holds; stopping"
        )
    if isinstance(original, QueryCanceled):
        seconds = STARTUP_STATEMENT_TIMEOUT_MS // 1000
        return RuntimeError(f"{step}: a statement ran longer than {seconds} s; stopping")
    return None


def _protect(engine: Engine) -> None:
    event.listen(engine, "handle_error", withhold_row_values, retval=True)


def _create_async_engine(url: URL, *, startup: bool = False) -> AsyncEngine:
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args=_connect_args(startup),
        **SAFE_ENGINE_OPTIONS,
    )
    _protect(engine.sync_engine)
    return engine


def create_database_engine(settings: DatabaseSettings) -> AsyncEngine:
    """The application's engine: it connects as the runtime role (app/db/roles.py)."""
    return _create_async_engine(settings.runtime_database_url)


# The owner's engines are for the start-up steps only: migrations, role set-up and the seed
# (a repo hygiene test keeps them there).


def create_owner_engine(settings: StartupSettings) -> AsyncEngine:
    """The owner, asynchronous: for the seed."""
    return _create_async_engine(settings.owner_database_url, startup=True)


def create_owner_sync_engine(settings: StartupSettings, **options: Any) -> Engine:
    """The owner, synchronous: for Alembic, role set-up and test set-up."""
    engine = create_engine(
        settings.owner_database_url,
        connect_args=_connect_args(startup=True),
        **options,
        **SAFE_ENGINE_OPTIONS,
    )
    _protect(engine)
    return engine


# What libpq's PQencryptPasswordConn returns for scram-sha-256 (RFC 5803's form):
# SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>, the last three in base64.
_BASE64 = "[A-Za-z0-9+/]+={0,2}"
SCRAM_VERIFIER = re.compile(rf"SCRAM-SHA-256\$[1-9][0-9]*:{_BASE64}\${_BASE64}:{_BASE64}")


def _scram_verifier(pgconn: Any, role: str, password: SecretStr) -> str:
    """libpq's SCRAM-SHA-256 verifier of `password` for `role`, computed on this side."""
    verifier: str = pgconn.encrypt_password(
        password.get_secret_value().encode("utf-8"), role.encode("utf-8"), b"scram-sha-256"
    ).decode("ascii")
    return verifier


def set_role_password(connection: Connection, role: str, password: SecretStr) -> None:
    """Set `role`'s password inside the connection's transaction, without sending the password.

    The statement carries the password's SCRAM-SHA-256 verifier, the form in which Postgres
    stores a password and which it stores as given. psycopg's `PGconn.encrypt_password` calls
    libpq's PQencryptPasswordConn: with the algorithm named it computes the verifier on this
    side (a fresh random salt each time, the iteration count the server reported) and sends
    nothing; without one it would ask the server for its `password_encryption` setting, which
    may be md5. A verifier alone does not log in, but whoever holds it can guess the password
    offline, can pose as the server to a client of this role, and with one recorded login
    exchange of the role can log in as it. So the statement goes straight to the driver
    connection, past SQLAlchemy: its
    statement log, events and error messages never see it, psycopg logs no statements, and a
    driver error does not quote the statement. The error is scrubbed like the engines' errors.
    """
    driver = connection.connection.driver_connection
    if driver is None:
        raise RuntimeError("the connection has no driver connection")
    verifier = _scram_verifier(driver.pgconn, role, password)
    # Anything else (an md5 hash, a quote, a second statement) is refused before it is sent.
    if not SCRAM_VERIFIER.fullmatch(verifier):
        raise RuntimeError("the driver did not return a SCRAM-SHA-256 verifier")
    statement = sql.SQL("ALTER ROLE {} PASSWORD {}").format(
        sql.Identifier(role), sql.Literal(verifier)
    )
    try:
        driver.execute(statement)
    except DriverError as error:
        _withhold_driver_error(error, None)
        raise


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def in_database_transaction(session: AsyncSession) -> bool:
    """Whether the session's connection is inside a database transaction, as the server said
    in its reply to the last statement (libpq keeps that status; no query is sent).

    SQLAlchemy cannot answer this: its session and connection report a transaction in
    autocommit mode too, because there the begin is only logical, and its isolation level
    query never reports autocommit. Only meaningful after a statement on this transaction.
    """
    raw = await (await session.connection()).get_raw_connection()
    driver = raw.driver_connection
    return driver is not None and driver.info.transaction_status == TransactionStatus.INTRANS
