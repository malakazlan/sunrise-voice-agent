import asyncio
import hashlib
import itertools
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import pytest
from conftest import TEST_AUDIT_KEY
from psycopg import errors
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.engine.interfaces import ExecutionContext
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.audit import log
from app.audit.log import (
    CANONICAL_VERSION,
    ZERO_MAC,
    Action,
    AuditLog,
    ChainBreak,
    ChainRow,
    Event,
    Outcome,
    Reason,
    ResourceType,
    Verification,
    canonical_form,
)
from app.config import StartupSettings
from app.db import session as session_module
from app.db.session import create_database_engine, create_owner_sync_engine, create_session_factory

pytestmark = pytest.mark.anyio

Tamper = Callable[..., None]

NEW_YORK = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 24, 15, 30, 0, 1, tzinfo=UTC)

ROWS = text(
    "SELECT seq, ts, call_id, actor, action, resource_type, resource_id, outcome, reason,"
    " prev_mac, mac FROM audit_events ORDER BY seq"
)
COUNT = text("SELECT count(*) FROM audit_events")
INSERT_ROW = text(
    "INSERT INTO audit_events (seq, ts, call_id, actor, action, resource_type, resource_id,"
    " outcome, reason, prev_mac, mac) VALUES (:seq, :ts, :call_id, :actor, :action,"
    " :resource_type, :resource_id, :outcome, :reason, :prev_mac, :mac)"
)
INSERT_PATIENT = text(
    "INSERT INTO patients (id, first_name, last_name, dob, phone, lab_results_status, balance_due)"
    " VALUES ('T001', 'Test', 'Patient', '1980-01-01', '555-000-0000', 'Pending', 0)"
)
PATIENTS = text("SELECT count(*) FROM patients")
# Sessions of this database that are waiting for an advisory lock.
LOCK_WAITERS = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)
# An owner-level way to make an INSERT store nothing, created past the table's protection.
SWALLOW_BY_TRIGGER = (
    "CREATE FUNCTION swallow() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$",
    "CREATE TRIGGER swallow BEFORE INSERT ON audit_events FOR EACH ROW EXECUTE FUNCTION swallow()",
)
# The reviewer's redirect: inserts go to another table, and the rule still returns the row.
REDIRECT_BY_RULE = (
    "CREATE TABLE audit_sink (LIKE audit_events)",
    "CREATE RULE redirect AS ON INSERT TO audit_events DO INSTEAD"
    " INSERT INTO audit_sink VALUES (NEW.*) RETURNING audit_sink.*",
)


def cid(number: int) -> str:
    """A call id in the format calls are given (uuid4 hex), distinct per number."""
    return f"{number:012x}4{number:03x}8{number:015x}"


def call_started(call_id: str | None = None) -> Event:
    call_id = cid(1) if call_id is None else call_id
    return Event(
        actor="agent",
        action=Action.CALL_STARTED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.SUCCESS,
        call_id=call_id,
        resource_id=call_id,
    )


def call_ended(call_id: str | None = None) -> Event:
    call_id = cid(1) if call_id is None else call_id
    return Event(
        actor="agent",
        action=Action.CALL_ENDED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.FAILURE,
        call_id=call_id,
        resource_id=call_id,
        reason=Reason.CALLER_HUNG_UP,
    )


# Event 1 has no reason; event 2 has every column set.
THREE = [call_started(cid(1)), call_ended(cid(1)), call_started(cid(2))]


@pytest.fixture
def audit(database: StartupSettings) -> AuditLog:
    return AuditLog(SecretStr(TEST_AUDIT_KEY))


@pytest.fixture
async def factory(database: StartupSettings) -> AsyncIterator[Callable[[], AsyncSession]]:
    """Sessions on one engine, each with its own connection."""
    engine = create_database_engine(database)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def append_all(
    audit: AuditLog, session: AsyncSession, events: Sequence[Event], start: datetime = T0
) -> list[int]:
    seqs = []
    for offset, item in enumerate(events):
        seqs.append(await audit.append(session, item, now=start + timedelta(seconds=offset)))
    await session.commit()
    return seqs


async def stored(session: AsyncSession) -> list[tuple[Any, ...]]:
    rows = [tuple(row) for row in (await session.execute(ROWS)).all()]
    await session.commit()
    return rows


async def verified(audit: AuditLog, session: AsyncSession) -> Verification:
    result = await audit.verify(session)
    await session.commit()
    return result


def broken(checked: int, *breaks: tuple[int | None, str], truncated: bool = False) -> Verification:
    """A failed verification: `checked` events read, then every break as (seq, kind)."""
    return Verification(
        ok=False,
        checked=checked,
        breaks=tuple(log.Break(seq=seq, kind=ChainBreak(kind)) for seq, kind in breaks),
        truncated=truncated,
    )


def intact(checked: int) -> Verification:
    return Verification(ok=True, checked=checked, breaks=(), truncated=False)


MAC = "mac_mismatch"
PREV = "prev_mac_mismatch"
GAP = "gap"
DUPLICATE = "duplicate"
WRONG_START = "wrong_start"
UNREADABLE = "unreadable"


async def lock_waiters(session: AsyncSession) -> int:
    count: int = (await session.execute(LOCK_WAITERS)).scalar_one()
    return count


async def wait_for_lock_waiters(session: AsyncSession, expected: int) -> None:
    with anyio.fail_after(10):
        while await lock_waiters(session) < expected:
            await session.rollback()
            await anyio.sleep(0.01)
    await session.rollback()


def counts(owner_engine: Engine) -> tuple[int, int]:
    """(patients, audit events) as committed."""
    with owner_engine.connect() as connection:
        return (
            connection.execute(PATIENTS).scalar_one(),
            connection.execute(COUNT).scalar_one(),
        )


# --- Neighbours ---------------------------------------------------------------------------


async def test_empty_log_verifies(audit: AuditLog, session: AsyncSession) -> None:
    assert await verified(audit, session) == intact(0)


async def test_first_event_starts_the_chain(audit: AuditLog, session: AsyncSession) -> None:
    assert await append_all(audit, session, [call_started()]) == [1]

    [row] = await stored(session)

    assert row[0] == 1
    assert row[9] == "0" * 64
    assert ZERO_MAC == "0" * 64
    assert re.fullmatch("[0-9a-f]{64}", row[10])


async def test_two_appends_in_one_transaction_are_chained(
    audit: AuditLog, session: AsyncSession
) -> None:
    await audit.append(session, call_started(), now=T0)
    await audit.append(session, call_ended(), now=T0)
    await session.commit()

    first, second = await stored(session)

    assert (first[0], second[0]) == (1, 2)
    assert second[9] == first[10]
    assert await verified(audit, session) == intact(2)


async def test_ts_round_trip_is_exact(audit: AuditLog, session: AsyncSession) -> None:
    # A clinic-local instant with every microsecond digit set.
    now = datetime(2026, 7, 1, 9, 5, 9, 123456, tzinfo=NEW_YORK)

    await append_all(audit, session, [call_started()], start=now)

    [row] = await stored(session)
    assert row[1] == datetime(2026, 7, 1, 13, 5, 9, 123456, tzinfo=UTC)
    assert await verified(audit, session) == intact(1)


async def test_clock_going_backwards_is_allowed(audit: AuditLog, session: AsyncSession) -> None:
    await audit.append(session, call_started(), now=T0)
    await audit.append(session, call_ended(), now=T0 - timedelta(hours=1))
    await session.commit()

    assert [row[0] for row in await stored(session)] == [1, 2]
    assert await verified(audit, session) == intact(2)


async def test_writer_waits_for_an_uncommitted_append(
    audit: AuditLog, factory: Callable[[], AsyncSession]
) -> None:
    second_seq: list[int] = []
    async with factory() as first, factory() as second, factory() as monitor:
        assert await audit.append(first, call_started(), now=T0) == 1

        async def second_writer() -> None:
            second_seq.append(await audit.append(second, call_ended(), now=T0))
            await second.commit()

        async with anyio.create_task_group() as group:
            group.start_soon(second_writer)
            await wait_for_lock_waiters(monitor, 1)
            assert second_seq == []
            # The first writer gives up: its event and its seq are gone.
            await first.rollback()

        assert second_seq == [1]
        assert await verified(audit, monitor) == intact(1)


async def test_killed_writer_releases_the_lock(
    audit: AuditLog, database: StartupSettings, factory: Callable[[], AsyncSession]
) -> None:
    admin = create_owner_sync_engine(database, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    second_seq: list[int] = []
    try:
        async with factory() as first, factory() as second, factory() as monitor:
            await audit.append(first, call_started(), now=T0)
            pid = (await first.execute(text("SELECT pg_backend_pid()"))).scalar_one()

            async def second_writer() -> None:
                second_seq.append(await audit.append(second, call_ended(), now=T0))
                await second.commit()

            async with anyio.create_task_group() as group:
                group.start_soon(second_writer)
                await wait_for_lock_waiters(monitor, 1)
                with admin.connect() as killer:
                    killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

            with pytest.raises(OperationalError):
                await first.commit()

            assert second_seq == [1]
            assert await verified(audit, monitor) == intact(1)
    finally:
        admin.dispose()


async def test_verify_reads_in_batches(
    audit: AuditLog,
    database: StartupSettings,
    owner_engine: Engine,
    tamper_with_audit_events: Tamper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(log, "VERIFY_BATCH_SIZE", 2)
    engine = create_database_engine(database)
    requested: list[dict[str, Any]] = []

    def record(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if statement.endswith("FROM audit_events ORDER BY audit_events.seq"):
            requested.append(dict(context.execution_options))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with create_session_factory(engine)() as session:
            await append_all(audit, session, [call_started(cid(n)) for n in range(5)])
            assert await verified(audit, session) == intact(5)

            # Event 3 opens the second batch; its removal is seen across the batch boundary.
            tamper_with_audit_events(owner_engine, "DELETE FROM audit_events WHERE seq = 3")
            assert await verified(audit, session) == broken(4, (4, GAP))
    finally:
        await engine.dispose()

    assert len(requested) == 2
    for options in requested:
        assert options["stream_results"] is True
        assert options["yield_per"] == 2


async def test_duplicate_seq_is_detected(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    await append_all(audit, session, THREE)

    # Only possible after the owner drops the primary key: an exact copy of event 2.
    tamper_with_audit_events(
        owner_engine,
        "ALTER TABLE audit_events DROP CONSTRAINT pk_audit_events",
        "INSERT INTO audit_events SELECT * FROM audit_events WHERE seq = 2",
    )

    assert await verified(audit, session) == broken(4, (2, DUPLICATE))


async def test_event_spliced_from_another_chain_is_detected(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    # A second chain under the same key, for example a copy of the database, supplies an
    # event 2 whose own MAC is genuine but which follows a different event 1.
    await append_all(audit, session, [call_started(cid(7)), call_ended(cid(7))])
    foreign = (await stored(session))[1]
    tamper_with_audit_events(owner_engine, "TRUNCATE audit_events")

    await append_all(audit, session, THREE)
    tamper_with_audit_events(
        owner_engine,
        "DELETE FROM audit_events WHERE seq = 2",
        (INSERT_ROW.text, dict(zip(ROW_COLUMNS, foreign, strict=True))),
    )

    # Event 2 does not follow event 1, and event 3 does not follow the foreign event 2.
    assert await verified(audit, session) == broken(3, (2, PREV), (3, PREV))


ROW_COLUMNS = (
    "seq",
    "ts",
    "call_id",
    "actor",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "reason",
    "prev_mac",
    "mac",
)


# --- R1 to R5: tampering is detected -------------------------------------------------------

# One edit per stored column, each made past the table's own protection, and every break
# verification must then report. The seq edit renumbers the last event: its MAC fails and so
# does its position. Editing a MAC breaks its own row and, because the walk goes on from the
# stored MAC, the link of the row after it.
EDITS = {
    "seq": ("UPDATE audit_events SET seq = 4 WHERE seq = 3", [(4, MAC), (4, GAP)]),
    "ts": ("UPDATE audit_events SET ts = ts + interval '1 microsecond' WHERE seq = 2", [(2, MAC)]),
    "call_id": ("UPDATE audit_events SET call_id = md5('nine') WHERE seq = 2", [(2, MAC)]),
    "call_id_to_null": ("UPDATE audit_events SET call_id = NULL WHERE seq = 2", [(2, MAC)]),
    "actor": ("UPDATE audit_events SET actor = 'admin:ops' WHERE seq = 2", [(2, MAC)]),
    "action": ("UPDATE audit_events SET action = 'call_started' WHERE seq = 2", [(2, MAC)]),
    "resource_type": (
        "UPDATE audit_events SET resource_type = 'patient' WHERE seq = 2",
        [(2, MAC)],
    ),
    "resource_id": ("UPDATE audit_events SET resource_id = md5('nine') WHERE seq = 2", [(2, MAC)]),
    "resource_id_to_null": ("UPDATE audit_events SET resource_id = NULL WHERE seq = 2", [(2, MAC)]),
    "outcome": ("UPDATE audit_events SET outcome = 'success' WHERE seq = 2", [(2, MAC)]),
    "reason": ("UPDATE audit_events SET reason = 'timeout' WHERE seq = 2", [(2, MAC)]),
    "reason_to_null": ("UPDATE audit_events SET reason = NULL WHERE seq = 2", [(2, MAC)]),
    "reason_from_null": (
        "UPDATE audit_events SET reason = 'caller_hung_up' WHERE seq = 1",
        [(1, MAC)],
    ),
    "prev_mac": ("UPDATE audit_events SET prev_mac = repeat('0', 64) WHERE seq = 2", [(2, MAC)]),
    "prev_mac_not_ascii": (
        "UPDATE audit_events SET prev_mac = repeat(chr(233), 64) WHERE seq = 2",
        [(2, MAC)],
    ),
    "mac": ("UPDATE audit_events SET mac = repeat('f', 64) WHERE seq = 2", [(2, MAC), (3, PREV)]),
}


@pytest.mark.parametrize("column", EDITS)
async def test_edit_to_any_column_is_detected(
    audit: AuditLog,
    session: AsyncSession,
    owner_engine: Engine,
    tamper_with_audit_events: Tamper,
    column: str,
) -> None:
    statement, breaks = EDITS[column]
    await append_all(audit, session, THREE)
    assert await verified(audit, session) == intact(3)
    before = await stored(session)

    tamper_with_audit_events(owner_engine, statement)

    assert await stored(session) != before
    assert await verified(audit, session) == broken(3, *breaks)


DELETIONS = {
    "middle": (["DELETE FROM audit_events WHERE seq = 2"], 3, [(3, GAP)]),
    "first": (["DELETE FROM audit_events WHERE seq = 1"], 3, [(2, WRONG_START)]),
    "two": (["DELETE FROM audit_events WHERE seq IN (2, 3)"], 2, [(4, GAP)]),
}


@pytest.mark.parametrize("case", DELETIONS)
async def test_deleted_event_is_detected(
    audit: AuditLog,
    session: AsyncSession,
    owner_engine: Engine,
    tamper_with_audit_events: Tamper,
    case: str,
) -> None:
    statements, checked, breaks = DELETIONS[case]
    await append_all(audit, session, [*THREE, call_ended(cid(2))])

    tamper_with_audit_events(owner_engine, *statements)

    assert await verified(audit, session) == broken(checked, *breaks)


async def test_first_event_must_follow_zeros(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    # Only a key holder can write this: event 1 with a genuine MAC that does not start from
    # 64 zeros. Its position is right; its link is not.
    genesis = ChainRow(
        seq=1,
        ts=T0,
        call_id=cid(1),
        actor="agent",
        action="call_started",
        resource_type="call",
        resource_id=cid(1),
        outcome="success",
        reason=None,
        prev_mac="ab" * 32,
    )
    values = {name: getattr(genesis, name) for name in ROW_COLUMNS[:-1]}
    tamper_with_audit_events(
        owner_engine, (INSERT_ROW.text, {**values, "mac": audit._mac(genesis)})
    )

    assert await verified(audit, session) == broken(1, (1, WRONG_START))


REORDERINGS = {
    # Both moved rows fail their MAC; event 4 then follows the wrong neighbour.
    "swap": (
        [
            "UPDATE audit_events SET seq = 0 WHERE seq = 2",
            "UPDATE audit_events SET seq = 2 WHERE seq = 3",
            "UPDATE audit_events SET seq = 3 WHERE seq = 0",
        ],
        4,
        [(2, MAC), (3, MAC), (4, PREV)],
    ),
    "renumber_all": (
        ["UPDATE audit_events SET seq = seq + 10"],
        4,
        [(11, MAC), (11, WRONG_START), (12, MAC), (13, MAC), (14, MAC)],
    ),
    "close_a_gap": (
        [
            "DELETE FROM audit_events WHERE seq = 2",
            "UPDATE audit_events SET seq = 2 WHERE seq = 3",
            "UPDATE audit_events SET seq = 3 WHERE seq = 4",
        ],
        3,
        [(2, MAC), (3, MAC)],
    ),
}


@pytest.mark.parametrize("case", REORDERINGS)
async def test_reordered_events_are_detected(
    audit: AuditLog,
    session: AsyncSession,
    owner_engine: Engine,
    tamper_with_audit_events: Tamper,
    case: str,
) -> None:
    statements, checked, breaks = REORDERINGS[case]
    await append_all(audit, session, [*THREE, call_ended(cid(2))])

    tamper_with_audit_events(owner_engine, *statements)

    assert await verified(audit, session) == broken(checked, *breaks)


def forged_row(seq: int, prev_mac: str) -> dict[str, Any]:
    """A row in the right place, made without the key: a plain hash of the documented input."""
    forged = ChainRow(
        seq=seq,
        ts=T0,
        call_id=cid(1),
        actor="admin:intruder",
        action="call_ended",
        resource_type="call",
        resource_id=cid(1),
        outcome="success",
        reason=None,
        prev_mac=prev_mac,
    )
    digest = hashlib.sha256(prev_mac.encode() + canonical_form(forged)).hexdigest()
    return {**{name: getattr(forged, name) for name in ROW_COLUMNS[:-1]}, "mac": digest}


async def test_forged_event_is_detected(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine
) -> None:
    await append_all(audit, session, THREE[:2])
    head_mac = (await stored(session))[-1][10]

    # Inserting needs no bypass. Without the key the forger can follow every other rule: the
    # next seq, the head's MAC as prev_mac, a hash of the documented input.
    with owner_engine.begin() as connection:
        connection.execute(INSERT_ROW, forged_row(3, head_mac))

    # A genuine event written afterwards chains onto the forgery; the break is still found.
    assert await append_all(audit, session, [call_started(cid(3))]) == [4]
    assert await verified(audit, session) == broken(4, (3, MAC))


async def test_wrong_key_fails_verification(audit: AuditLog, session: AsyncSession) -> None:
    await append_all(audit, session, THREE)
    other_key = "-".join(["other", "audit", "key"] * 4)
    other = AuditLog(SecretStr(other_key))

    assert await verified(other, session) == broken(3, (1, MAC), (2, MAC), (3, MAC))
    assert await verified(audit, session) == intact(3)
    assert other_key not in repr(other)
    with pytest.raises(ValueError, match="key"):
        AuditLog(SecretStr(""))


# --- F2: position enforced by the database, and every break reported ---------------------


def position_refusal(error: DBAPIError) -> str | None:
    """The column the position trigger refused on, or fail if something else refused."""
    refusal = error.orig
    assert isinstance(refusal, errors.IntegrityConstraintViolation), type(refusal)
    assert refusal.sqlstate == "23000", refusal.sqlstate
    assert refusal.diag.table_name == "audit_events"
    return refusal.diag.column_name


async def test_database_refuses_out_of_position_insert(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine
) -> None:
    def insert(seq: int, prev_mac: str) -> IntegrityError:
        with pytest.raises(IntegrityError) as excinfo, owner_engine.begin() as connection:
            connection.execute(INSERT_ROW, forged_row(seq, prev_mac))
        return excinfo.value

    # An empty table takes only seq 1 after 64 zeros.
    assert position_refusal(insert(2, ZERO_MAC)) == "seq"
    assert position_refusal(insert(1, "ab" * 32)) == "prev_mac"

    await append_all(audit, session, THREE[:2])
    head_mac = (await stored(session))[-1][10]
    for seq in (2**63 - 1, 4, 2, 1):  # the top of bigint, a gap, a repeat, the start again
        assert position_refusal(insert(seq, head_mac)) == "seq", seq
    assert position_refusal(insert(3, ZERO_MAC)) == "prev_mac"

    # The next position is accepted, and genuine appends go on after it.
    with owner_engine.begin() as connection:
        connection.execute(INSERT_ROW, forged_row(3, head_mac))
    assert await append_all(audit, session, [call_started(cid(3))]) == [4]


def test_position_trigger_cannot_be_shadowed(owner_engine: Engine) -> None:
    # Objects created later, ahead of public and pg_catalog on a session's search path, do not
    # change what the position trigger reads: public.audit_events, and pg_catalog's functions.
    with owner_engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA shadow"))
        connection.execute(text("CREATE TABLE shadow.audit_events (seq bigint, mac char(64))"))
        connection.execute(text("INSERT INTO shadow.audit_events VALUES (41, repeat('f', 64))"))
        connection.execute(
            text(
                "CREATE FUNCTION shadow.repeat(text, integer) RETURNS text LANGUAGE sql"
                " AS $$ SELECT pg_catalog.repeat('f', $2) $$"
            )
        )

    def insert(seq: int, prev_mac: str) -> None:
        with owner_engine.begin() as connection:
            connection.execute(text("SET LOCAL search_path = shadow, pg_catalog, public"))
            connection.execute(
                text(INSERT_ROW.text.replace("INTO audit_events", "INTO public.audit_events")),
                forged_row(seq, prev_mac),
            )

    # The real table is empty: seq 1 after 64 zeros, whatever the shadows say.
    insert(1, ZERO_MAC)
    with pytest.raises(IntegrityError) as shadowed:
        insert(42, "f" * 64)
    assert position_refusal(shadowed.value) == "seq"


async def test_verify_reports_every_break(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    # A junk row at head + 1 needs only INSERT. Genuine events follow it, and a later one is
    # edited: both breaks are found, and nothing else.
    await append_all(audit, session, THREE[:2])
    head_mac = (await stored(session))[-1][10]
    with owner_engine.begin() as connection:
        connection.execute(INSERT_ROW, forged_row(3, head_mac))
    assert await append_all(audit, session, [call_started(cid(3)), call_ended(cid(3))]) == [4, 5]
    tamper_with_audit_events(
        owner_engine, "UPDATE audit_events SET outcome = 'success' WHERE seq = 5"
    )

    assert await verified(audit, session) == broken(5, (3, MAC), (5, MAC))


async def test_verify_caps_the_list_of_breaks(
    audit: AuditLog, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await append_all(audit, session, THREE)
    monkeypatch.setattr(log, "MAX_REPORTED_BREAKS", 2)
    other = AuditLog(SecretStr("-".join(["other", "audit", "key"] * 4)))

    # Every event is checked; the list stops at the cap and says so.
    assert await verified(other, session) == broken(3, (1, MAC), (2, MAC), truncated=True)
    assert log.MAX_REPORTED_BREAKS == 2
    monkeypatch.undo()
    assert log.MAX_REPORTED_BREAKS == 100


async def test_verify_reports_unreadable_rows_and_goes_on(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    # One batch: an edited row, a timestamp at infinity, one beyond the year 9999 that the
    # driver can represent, a MAC made null by the owner, a genuine row whose link to that
    # null MAC fails, and a seq made null (sorted last), which has no seq to report and moves
    # the expected position nowhere. verify raises for none of them.
    await append_all(
        audit, session, [*THREE, call_ended(cid(2)), call_started(cid(3)), call_ended(cid(3))]
    )
    tamper_with_audit_events(
        owner_engine,
        "UPDATE audit_events SET outcome = 'failure' WHERE seq = 1",
        "UPDATE audit_events SET ts = 'infinity' WHERE seq = 2",
        "UPDATE audit_events SET ts = '20000-01-01 00:00:00+00' WHERE seq = 3",
        "ALTER TABLE audit_events ALTER COLUMN mac DROP NOT NULL",
        "UPDATE audit_events SET mac = NULL WHERE seq = 4",
        "ALTER TABLE audit_events DROP CONSTRAINT pk_audit_events",
        "ALTER TABLE audit_events ALTER COLUMN seq DROP NOT NULL",
        "UPDATE audit_events SET seq = NULL WHERE seq = 6",
    )

    assert await verified(audit, session) == broken(
        6,
        (1, MAC),
        (2, UNREADABLE),
        (3, UNREADABLE),
        (4, UNREADABLE),
        (5, PREV),
        (None, UNREADABLE),
    )


async def test_verify_reports_a_gap_next_to_an_edit(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    # Event 2 removed and event 3 edited: event 3 fails its MAC and its position. A row can
    # carry more than one break.
    await append_all(audit, session, [*THREE, call_ended(cid(2))])
    tamper_with_audit_events(
        owner_engine,
        "DELETE FROM audit_events WHERE seq = 2",
        "UPDATE audit_events SET outcome = 'failure' WHERE seq = 3",
    )

    assert await verified(audit, session) == broken(3, (3, MAC), (3, GAP))


# --- R6: concurrency ----------------------------------------------------------------------

WORKERS = 8
APPENDS_EACH = 5


async def test_concurrent_appends_keep_one_chain(
    audit: AuditLog, factory: Callable[[], AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The race is forced, not left to timing: after reading the head, each writer waits until
    # every writer that is still working has either read the head too or is queued on the
    # advisory lock. With the lock, the others queue and the holder goes on alone. Without it,
    # all of them read the same head before any of them inserts.
    read_head = log._chain_head
    remaining = WORKERS
    holding_head = 0

    async def head_held_open(session: AsyncSession) -> tuple[int, str] | None:
        nonlocal holding_head
        head = await read_head(session)
        holding_head += 1
        try:
            with anyio.fail_after(30):
                while holding_head + await lock_waiters(session) < remaining:
                    await anyio.sleep(0.005)
        finally:
            holding_head -= 1
        return head

    monkeypatch.setattr(log, "_chain_head", head_held_open)
    seqs: list[int] = []
    failures: list[str] = []

    async def writer(number: int) -> None:
        nonlocal remaining
        try:
            async with factory() as session:
                for index in range(APPENDS_EACH):
                    item = call_started(cid(number * 16 + index))
                    seqs.append(await audit.append(session, item, now=T0))
                    await session.commit()
        except Exception as error:  # noqa: BLE001 (collected and asserted below)
            sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
            failures.append(f"{type(error).__name__} {sqlstate}")
        finally:
            remaining -= 1

    async with anyio.create_task_group() as group:
        for number in range(WORKERS):
            group.start_soon(writer, number)

    assert failures == []
    assert sorted(seqs) == list(range(1, WORKERS * APPENDS_EACH + 1))
    monkeypatch.undo()
    async with factory() as session:
        assert await verified(audit, session) == intact(WORKERS * APPENDS_EACH)


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
async def test_contended_append_fails_closed_above_read_committed(
    audit: AuditLog, factory: Callable[[], AsyncSession], level: str
) -> None:
    # The snapshot is taken before another writer commits, so the head it sees is stale: the
    # append fails on the primary key instead of writing a second event 1. Callers use the
    # default isolation level, READ COMMITTED.
    async with factory() as early, factory() as other:
        await early.connection(execution_options={"isolation_level": level})
        await early.execute(text("SELECT 1"))
        await append_all(audit, other, [call_started()])

        with pytest.raises(IntegrityError) as excinfo:
            await audit.append(early, call_ended(), now=T0)
        assert isinstance(excinfo.value.orig, errors.UniqueViolation)
        assert excinfo.value.orig.diag.constraint_name == "pk_audit_events"

        await early.commit()
        assert await verified(audit, other) == intact(1)


# --- R7, R8, F1, F3, F4, F5, F12: the event and its action commit together -----------------


async def test_event_rolls_back_with_the_action(audit: AuditLog, session: AsyncSession) -> None:
    await session.execute(INSERT_PATIENT)
    await audit.append(session, call_started(), now=T0)
    await audit.append(session, call_ended(), now=T0)
    await session.rollback()

    assert (await session.execute(PATIENTS)).scalar_one() == 0
    assert (await session.execute(COUNT)).scalar_one() == 0
    await session.rollback()

    # The rolled-back appends consumed no seq.
    assert await append_all(audit, session, [call_started()]) == [1]
    assert await verified(audit, session) == intact(1)


async def test_append_refuses_a_session_outside_a_transaction(
    audit: AuditLog, database: StartupSettings, owner_engine: Engine
) -> None:
    # In autocommit every statement commits on its own: the lock would end at once and the
    # event could not roll back with its action. Set on the engine and on one connection.
    engine = create_database_engine(database)
    try:
        autocommit_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
        async with create_session_factory(autocommit_engine)() as session:
            with pytest.raises(RuntimeError, match="transaction"):
                await audit.append(session, call_started(), now=T0)

        async with create_session_factory(engine)() as session:
            await session.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
            with pytest.raises(RuntimeError, match="transaction"):
                await audit.append(session, call_started(), now=T0)

        # The same engine without autocommit appends as usual.
        async with create_session_factory(engine)() as session:
            assert await append_all(audit, session, [call_started()]) == [1]
    finally:
        await engine.dispose()

    with owner_engine.connect() as connection:
        assert connection.execute(COUNT).scalar_one() == 1


def rewrite_on_insert(assignment: str) -> tuple[str, str]:
    """An owner's BEFORE INSERT trigger that changes the row on its way into storage. Named
    `rewrite`, it fires after `audit_events_check_position` (same-kind triggers fire in name
    order), and changes neither seq nor prev_mac, so the row is stored.
    """
    return (
        "CREATE FUNCTION rewrite() RETURNS trigger LANGUAGE plpgsql AS"
        f" $$ BEGIN {assignment}; RETURN NEW; END $$",
        "CREATE TRIGGER rewrite BEFORE INSERT ON audit_events"
        " FOR EACH ROW EXECUTE FUNCTION rewrite()",
    )


async def test_action_fails_when_the_stored_mac_is_rewritten(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    # The MAC becomes another 64-hex value; everything else is stored as written.
    tamper_with_audit_events(owner_engine, *rewrite_on_insert("NEW.mac := repeat('f', 64)"))

    await session.execute(INSERT_PATIENT)
    with pytest.raises(RuntimeError, match="differs from what was written"):
        await audit.append(session, call_started(), now=T0)
    await session.commit()

    assert counts(owner_engine) == (0, 0)


async def test_action_fails_when_stored_content_is_rewritten(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    # The outcome becomes another registry value; the MAC is left as append computed it.
    tamper_with_audit_events(owner_engine, *rewrite_on_insert("NEW.outcome := 'failure'"))

    await session.execute(INSERT_PATIENT)
    with pytest.raises(RuntimeError, match="differs from what was written"):
        await audit.append(session, call_started(), now=T0)
    await session.commit()

    assert counts(owner_engine) == (0, 0)


async def test_action_fails_when_the_insert_is_redirected(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    tamper_with_audit_events(owner_engine, *REDIRECT_BY_RULE)

    # The rule returns the row, so only the read-back can tell.
    await session.execute(INSERT_PATIENT)
    with pytest.raises(RuntimeError, match="not in audit_events"):
        await audit.append(session, call_started(), now=T0)
    await session.commit()

    assert counts(owner_engine) == (0, 0)
    with owner_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM audit_sink")).scalar_one() == 0


async def test_action_fails_when_the_insert_is_swallowed(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    # A trigger that returns NULL makes the INSERT store nothing and return nothing; the
    # RETURNING check sees it first. (A DO INSTEAD NOTHING rule needs no test of its own:
    # Postgres refuses INSERT ... RETURNING on such a table before append's checks run.)
    tamper_with_audit_events(owner_engine, *SWALLOW_BY_TRIGGER)

    await session.execute(INSERT_PATIENT)
    with pytest.raises(RuntimeError, match="returned no row"):
        await audit.append(session, call_started(), now=T0)
    await session.commit()

    assert counts(owner_engine) == (0, 0)


# Each caller below meets an append that fails while the database transaction is still
# healthy (the INSERT stores nothing), so only append's own rollback keeps the action out.


async def ignore_the_error_and_commit(audit: AuditLog, session: AsyncSession) -> None:
    await session.execute(INSERT_PATIENT)
    with pytest.raises(RuntimeError, match="not stored"):
        await audit.append(session, call_started(), now=T0)
    await session.commit()


async def catch_it_inside_a_transaction_block(audit: AuditLog, session: AsyncSession) -> None:
    # The block commits on leaving; append has already ended its transaction, so there is
    # nothing left to commit.
    async with session.begin():
        await session.execute(INSERT_PATIENT)
        with pytest.raises(RuntimeError, match="not stored"):
            await audit.append(session, call_started(), now=T0)


CALLERS = {
    "ignore_the_error_and_commit": ignore_the_error_and_commit,
    "catch_it_inside_a_transaction_block": catch_it_inside_a_transaction_block,
}


@pytest.mark.parametrize("caller", [*CALLERS, "inside_a_savepoint"])
async def test_action_fails_when_event_cannot_be_written(
    audit: AuditLog,
    session: AsyncSession,
    owner_engine: Engine,
    tamper_with_audit_events: Tamper,
    caller: str,
) -> None:
    if caller == "inside_a_savepoint":
        # A savepoint could roll the event back while the action commits: append refuses to
        # run inside one, and rolls back the whole transaction.
        await session.execute(INSERT_PATIENT)
        with pytest.raises(RuntimeError, match="nested"):
            async with session.begin_nested():
                await audit.append(session, call_started(), now=T0)
        await session.commit()
    else:
        tamper_with_audit_events(owner_engine, *SWALLOW_BY_TRIGGER)
        await CALLERS[caller](audit, session)

    assert counts(owner_engine) == (0, 0)


@dataclass(frozen=True)
class LookAlike:
    """Has every field of an event, each valid, but is not an event."""

    actor: str = "agent"
    action: Action = Action.CALL_STARTED
    resource_type: ResourceType = ResourceType.CALL
    outcome: Outcome = Outcome.SUCCESS
    call_id: str | None = None
    resource_id: str | None = None
    reason: Reason | None = None


def ssn_shaped() -> str:
    return "-".join(["9" * 3, "8" * 2, "7" * 4])


def mutated(field: str, value: object) -> Event:
    item = call_started()
    object.__setattr__(item, field, value)
    return item


async def test_append_revalidates_the_event(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine
) -> None:
    attempts: list[tuple[object, type[Exception]]] = [
        (LookAlike(), TypeError),
        (mutated("action", "unregistered_action"), TypeError),
        (mutated("actor", "free text"), ValueError),
        (mutated("call_id", ssn_shaped()), ValueError),
        (mutated("resource_id", "2026-11-24"), ValueError),
    ]
    for item, error in attempts:
        await session.execute(INSERT_PATIENT)
        with pytest.raises(error):
            await audit.append(session, item, now=T0)  # type: ignore[arg-type]
        await session.commit()

        assert counts(owner_engine) == (0, 0), item


async def test_cancelled_append_rolls_back_the_action(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The caller hangs up after the head is read; the caller's own `finally` then commits.
    # The second time, the task is cancelled again while the rollback is running.
    read_head = log._chain_head
    head_read = asyncio.Event()

    async def head_then_wait(session: AsyncSession) -> tuple[int, str] | None:
        head = await read_head(session)
        head_read.set()
        await asyncio.Event().wait()
        return head

    monkeypatch.setattr(log, "_chain_head", head_then_wait)
    real_rollback = session.rollback
    rollback_started = asyncio.Event()

    async def slow_rollback() -> None:
        rollback_started.set()
        await asyncio.sleep(0.2)
        await real_rollback()

    monkeypatch.setattr(session, "rollback", slow_rollback)

    async def caller() -> None:
        try:
            await session.execute(INSERT_PATIENT)
            await audit.append(session, call_started(), now=T0)
        finally:
            await session.commit()

    for cancel_again in (False, True):
        head_read.clear()
        rollback_started.clear()
        task = asyncio.ensure_future(caller())
        with anyio.fail_after(10):
            await head_read.wait()
        task.cancel()
        if cancel_again:
            with anyio.fail_after(10):
                await rollback_started.wait()
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert counts(owner_engine) == (0, 0), cancel_again


async def test_failed_rollback_keeps_the_original_error(
    audit: AuditLog,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_rollback() -> None:
        raise ConnectionError("rollback failed")

    monkeypatch.setattr(session, "rollback", failing_rollback)
    caplog.set_level(logging.WARNING, logger="app.audit.log")

    with pytest.raises(ValueError, match="now"):
        await audit.append(session, call_started(), now=datetime(2026, 11, 24, 15, 30))

    assert [record.getMessage() for record in caplog.records] == [
        "audit_append_rollback_failed error=ConnectionError"
    ]


async def test_append_gives_up_when_the_lock_is_held(
    audit: AuditLog, factory: Callable[[], AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert log.LOCK_TIMEOUT_MS == 5000
    monkeypatch.setattr(log, "LOCK_TIMEOUT_MS", 200)
    async with factory() as holder, factory() as waiter:
        await audit.append(holder, call_started(), now=T0)
        await waiter.execute(INSERT_PATIENT)

        with anyio.fail_after(10), pytest.raises(OperationalError) as excinfo:
            await audit.append(waiter, call_ended(), now=T0)

        assert isinstance(excinfo.value.orig, errors.LockNotAvailable)
        await waiter.commit()
        assert (await waiter.execute(PATIENTS)).scalar_one() == 0
        await holder.rollback()


async def test_idle_transaction_is_ended_by_the_server(
    database: StartupSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    show = text("SHOW idle_in_transaction_session_timeout")
    engine = create_database_engine(database)
    sync = create_owner_sync_engine(database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(show)).scalar_one() == "30s"
        with sync.connect() as connection:
            assert connection.execute(show).scalar_one() == "30s"
    finally:
        await engine.dispose()
        sync.dispose()

    monkeypatch.setattr(session_module, "IDLE_IN_TRANSACTION_TIMEOUT_MS", 300)
    engine = create_database_engine(database)
    try:
        async with create_session_factory(engine)() as session:
            await session.execute(text("SELECT 1"))
            await anyio.sleep(1.5)
            with pytest.raises(DBAPIError) as excinfo:
                await session.execute(text("SELECT 1"))
            assert isinstance(excinfo.value.orig, errors.IdleInTransactionSessionTimeout)
    finally:
        await engine.dispose()


# --- R9, R10: the database's own rules ----------------------------------------------------


async def test_database_refuses_update_delete_truncate(
    audit: AuditLog, session: AsyncSession, owner_engine: Engine
) -> None:
    await append_all(audit, session, THREE)
    before = await stored(session)

    # The test role is a superuser and owns the table; it is refused all the same.
    for statement in (
        "UPDATE audit_events SET actor = 'admin:x' WHERE seq = 2",
        "UPDATE audit_events SET actor = 'admin:x' WHERE false",
        "INSERT INTO audit_events SELECT * FROM audit_events WHERE seq = 1"
        " ON CONFLICT (seq) DO UPDATE SET actor = 'admin:x'",
        "DELETE FROM audit_events WHERE seq = 3",
        "DELETE FROM audit_events WHERE false",
        "MERGE INTO audit_events USING (SELECT 1 AS seq) AS source"
        " ON audit_events.seq = source.seq WHEN MATCHED THEN DELETE",
        "TRUNCATE audit_events",
    ):
        with pytest.raises(ProgrammingError) as excinfo, owner_engine.begin() as connection:
            connection.execute(text(statement))
        refusal = excinfo.value.orig
        assert isinstance(refusal, errors.InsufficientPrivilege), statement
        assert refusal.diag.table_name == "audit_events"

    assert await stored(session) == before
    assert await verified(audit, session) == intact(3)


# Each value breaks the rule in one way: a space, one character too many, a character outside
# letters, digits, _ - : and ., a control character, a trailing newline, or nothing at all.
FREE_TEXT = [
    "has space",
    "x" * 65,
    "semi;colon",
    "slash/x",
    "quote'x",
    "caf\u00e9",
    "tab\tx",
    "new\nline",
    "trailing\n",
    "",
]
TEXT_COLUMNS = ("call_id", "actor", "action", "resource_type", "resource_id", "outcome", "reason")


def changed(item: Event, **changes: Any) -> Event:
    """`item` with some fields replaced, through the constructor and its checks."""
    return replace(item, **changes)


def test_fields_reject_free_text() -> None:
    base = call_started()
    for field in ("call_id", "resource_id"):
        for value in FREE_TEXT:
            with pytest.raises(ValueError) as excinfo:
                changed(base, **{field: value})
            assert field in str(excinfo.value)
            if len(value) > 2:
                assert value not in str(excinfo.value), (field, value)
        with pytest.raises(TypeError):
            changed(base, **{field: 7})
        assert getattr(changed(base, **{field: cid(5)}), field) == cid(5)
        assert getattr(changed(base, **{field: None}), field) is None

    for value in FREE_TEXT:
        with pytest.raises(ValueError) as excinfo:
            replace(base, actor=value)
        assert "actor" in str(excinfo.value)
        if len(value) > 2:
            assert value not in str(excinfo.value), value
    for actor in ("admin:", "admin", "Agent", "agent:x", "agent ", "root", "admin:has space"):
        with pytest.raises(ValueError, match="actor"):
            replace(base, actor=actor)
    with pytest.raises(ValueError, match="actor"):
        replace(base, actor="admin:" + "x" * 33)
    with pytest.raises(TypeError, match="actor"):
        changed(base, actor=None)
    for actor in ("agent", "admin:ops", "admin:ops_lead", "admin:" + "x" * 32):
        assert replace(base, actor=actor).actor == actor


# Literal rows: what each kind of identifier refuses, and one real example it accepts.
NOT_IDENTIFIERS = [
    ssn_shaped(),
    "".join(["9" * 3, "8" * 2, "7" * 4]),  # nine digits
    "2026-11-24",
    "5552013344",
    "555-201-3344",
    "mary_ann_lee",
]


def test_identifiers_must_fit_their_declared_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    base = call_started()
    real_call_id = uuid.uuid4().hex
    kinds: list[tuple[str, dict[str, Any], str]] = [
        ("call_id", {}, real_call_id),
        ("resource_id", {"resource_type": ResourceType.CALL}, real_call_id),
        ("resource_id", {"resource_type": log.ResourceType.PATIENT}, "P001"),
        ("resource_id", {"resource_type": log.ResourceType.APPOINTMENT}, "42"),
        ("resource_id", {"resource_type": log.ResourceType.MEDICATION}, "12345678"),
        ("resource_id", {"resource_type": log.ResourceType.SLOT}, "7"),
    ]
    for field, context, example in kinds:
        assert getattr(changed(base, **context, **{field: example}), field) == example
        for value in NOT_IDENTIFIERS:
            with pytest.raises(ValueError) as excinfo:
                changed(base, **context, **{field: value})
            assert field in str(excinfo.value)
            assert value not in str(excinfo.value), (field, context, value)
    for value in ("123456789", "P0001234567", "0", "042", "p001"):
        with pytest.raises(ValueError):
            changed(base, resource_type=log.ResourceType.MEDICATION, resource_id=value)
        with pytest.raises(ValueError):
            changed(base, resource_type=log.ResourceType.PATIENT, resource_id=value)

    # A call id is a version-4 UUID in hex: the version digit and the variant are checked.
    # Here 3f8e2a1c-9b7d-4e6f-8a1b-2c3d4e5f6a7b, then with version 1, then with variant c.
    version_4 = "".join(["3f8e2a1c", "9b7d", "4e6f", "8a1b", "2c3d4e5f6a7b"])
    for call_id in (
        "".join(["3f8e2a1c", "9b7d", "1e6f", "8a1b", "2c3d4e5f6a7b"]),
        "".join(["3f8e2a1c", "9b7d", "4e6f", "ca1b", "2c3d4e5f6a7b"]),
        version_4.upper(),
        str(uuid.UUID(version_4)),
    ):
        with pytest.raises(ValueError, match="call_id"):
            changed(base, call_id=call_id)
    assert changed(base, call_id=version_4).call_id == version_4

    # An admin user name is letters and underscores, starting with a letter: no digits, dots
    # or hyphens. The reviewer's values, the SSN-shaped one built from pieces.
    for actor in ("admin:mlee", "admin:ops_lead", "admin:Mary_Lee"):
        assert replace(base, actor=actor).actor == actor
    for actor in (
        "admin:x" + "-".join(["123", "45", "6789"]),
        "admin:x555-867-5309",
        "admin:dob-1980-01-01",
        "admin:john.smith",
        "admin:ops2",
        "admin:_ops",
        "admin:" + "x" * 33,
        *[f"admin:{value}" for value in NOT_IDENTIFIERS if value != "mary_ann_lee"],
    ):
        with pytest.raises(ValueError, match="actor"):
            replace(base, actor=actor)

    # A tool's id is its registered name: lower-case letters and underscores, 2 to 40.
    tool = {"resource_type": log.ResourceType.TOOL}
    for name in ("ab", "get_balance", "x" * 40, "__"):
        assert changed(base, **tool, resource_id=name).resource_id == name
    for name in ("a", "x" * 41, "Get_balance", "get balance", "get-balance", "tool2", "", "P001"):
        with pytest.raises(ValueError, match="resource_id"):
            changed(base, **tool, resource_id=name)
    for value in NOT_IDENTIFIERS:
        if value != "mary_ann_lee":
            with pytest.raises(ValueError, match="resource_id"):
                changed(base, **tool, resource_id=value)

    # A resource type without a declared pattern cannot be used.
    patterns = dict(log.ID_PATTERNS)
    del patterns[log.ResourceType.SLOT]
    monkeypatch.setattr(log, "ID_PATTERNS", patterns)
    with pytest.raises(ValueError, match="resource_type"):
        changed(base, resource_type=log.ResourceType.SLOT, resource_id="7")
    assert set(log.ResourceType) - set(patterns) == {log.ResourceType.SLOT}


def valid_row(seq: int) -> dict[str, Any]:
    return {
        "seq": seq,
        "ts": T0,
        "call_id": "call-1",
        "actor": "agent",
        "action": "call_started",
        "resource_type": "call",
        "resource_id": "call-1",
        "outcome": "success",
        "reason": "caller_hung_up",
        "prev_mac": "0" * 64,
        "mac": "0" * 64,
    }


def test_database_rejects_free_text(owner_engine: Engine) -> None:
    # Every row is placed at the next position (all MACs zero), so the CHECKs decide.
    accepted = 0
    for column in TEXT_COLUMNS:
        for value in FREE_TEXT:
            with pytest.raises(IntegrityError) as excinfo, owner_engine.begin() as connection:
                connection.execute(INSERT_ROW, {**valid_row(accepted + 1), column: value})
            refusal = excinfo.value.orig
            assert isinstance(refusal, errors.CheckViolation), (column, value)
            assert refusal.diag.constraint_name == f"ck_audit_events_{column}", (column, value)
            if len(value) > 2:
                assert value not in str(excinfo.value)

        # The longest allowed value, every allowed character, is accepted.
        accepted += 1
        with owner_engine.begin() as connection:
            connection.execute(INSERT_ROW, {**valid_row(accepted), column: "Az09_-:." + "x" * 56})

    with owner_engine.connect() as connection:
        assert connection.execute(COUNT).scalar_one() == len(TEXT_COLUMNS)


# --- R11, R12: registries and time --------------------------------------------------------


def test_only_registry_members_are_accepted() -> None:
    base = call_started()
    refused: list[dict[str, Any]] = [
        {"action": "call_started"},
        {"action": Outcome.SUCCESS},
        {"action": None},
        {"resource_type": "call"},
        {"resource_type": Action.CALL_STARTED},
        {"outcome": "success"},
        {"outcome": Reason.CALLER_HUNG_UP},
        {"outcome": None},
        {"reason": "caller_hung_up"},
        {"reason": Outcome.FAILURE},
    ]
    for change in refused:
        with pytest.raises(TypeError) as excinfo:
            changed(base, **change)
        assert next(iter(change)) in str(excinfo.value)

    assert replace(base, reason=Reason.CALLER_HUNG_UP).reason is Reason.CALLER_HUNG_UP
    assert replace(base, reason=None).reason is None

    # Every member so far, each a valid stored value.
    assert {member.value for member in Action} == {
        "call_started",
        "call_ended",
        "tool_called",
        "tool_refused",
        "tool_failed",
        "identity_claimed",
        "identity_verified",
        "identity_refused",
        "identity_locked",
        "appointment_read",
        "refill_requested",
        "lab_status_read",
        "balance_read",
        "doctor_message_sent",
        "admin_read_requests",
        "admin_read_messages",
        "admin_read_audit",
        "admin_verify_audit",
        "refill_previewed",
        "refill_not_found",
        "doctor_message_previewed",
    }
    assert {member.value for member in ResourceType} == {
        "call",
        "patient",
        "appointment",
        "medication",
        "slot",
        "tool",
        "request",
        "message",
        "audit_event",
    }
    assert {member.value for member in Outcome} == {"success", "failure", "refused"}
    assert {member.value for member in Reason} == {"caller_hung_up"}
    for registry in (Action, ResourceType, Outcome, Reason):
        for member in registry:
            assert re.fullmatch("[a-z0-9_]{1,64}", member.value)
    assert set(log.ID_PATTERNS) == set(ResourceType)


async def test_naive_timestamp_is_rejected(audit: AuditLog, session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="now"):
        await audit.append(session, call_started(), now=datetime(2026, 11, 24, 15, 30))
    for not_a_datetime in (date(2026, 11, 24), "2026-11-24T15:30:00Z", 1764000000):
        with pytest.raises(TypeError, match="now"):
            await audit.append(session, call_started(), now=not_a_datetime)  # type: ignore[arg-type]

    # Refused before the database was touched: no transaction, no lock, no row.
    assert not session.in_transaction()
    assert (await session.execute(COUNT)).scalar_one() == 0


# --- R13 to R15: the MAC and the canonical form -------------------------------------------

FAKE_KEY = "-".join(["fake", "audit", "key"] * 3)
KNOWN_CALL_ID = "".join(["3f8e2a1c", "9b7d4e6f", "8a1b2c3d", "4e5f6a7b"])

# Computed with openssl, not with the code under test (Git Bash or any POSIX shell). E1 and E2
# are the two events' canonical forms; each MAC is one command, over prev_mac then the form:
#   K=fake-audit-key-fake-audit-key-fake-audit-key; Z=$(printf '%064d' 0); V=16:sunrise-audit-v1
#   C=<KNOWN_CALL_ID>
#   E1="${V}1:127:2026-11-24T15:30:00.000001Z-5:agent12:call_started4:call-7:success-64:${Z}"
#   A=$(printf '%s' "$Z$E1" | openssl dgst -sha256 -hmac "$K" | cut -d' ' -f2)
#   E2="${V}1:227:2026-07-01T13:05:09.500000Z32:${C}14:admin:ops_lead10:call_ended4:call"
#   E2="${E2}32:${C}7:failure14:caller_hung_up64:${A}"
#   B=$(printf '%s' "$A$E2" | openssl dgst -sha256 -hmac "$K" | cut -d' ' -f2)
KNOWN_MAC_1 = "".join(
    "98091bc7 666c5dff 1b9b4e9f 073b8b66 621db182 cb347e1e f469dfb2 0c3f2b83".split()
)
KNOWN_MAC_2 = "".join(
    "49fa89f4 8f05d455 40df6eb0 d52c6bfd af4c777a c33cf4fb 330537a1 ddd0de64".split()
)


async def test_mac_matches_known_vector(session: AsyncSession) -> None:
    audit = AuditLog(SecretStr(FAKE_KEY))
    first = Event(
        actor="agent",
        action=Action.CALL_STARTED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.SUCCESS,
    )
    second = Event(
        actor="admin:ops_lead",
        action=Action.CALL_ENDED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.FAILURE,
        call_id=KNOWN_CALL_ID,
        resource_id=KNOWN_CALL_ID,
        reason=Reason.CALLER_HUNG_UP,
    )

    await audit.append(session, first, now=datetime(2026, 11, 24, 15, 30, 0, 1, tzinfo=UTC))
    await audit.append(session, second, now=datetime(2026, 7, 1, 9, 5, 9, 500000, tzinfo=NEW_YORK))
    await session.commit()

    one, two = await stored(session)
    assert (one[9], one[10]) == ("0" * 64, KNOWN_MAC_1)
    assert (two[9], two[10]) == (KNOWN_MAC_1, KNOWN_MAC_2)


WITH_NULLS = ChainRow(
    seq=1,
    ts=datetime(2026, 11, 24, 15, 30, 0, 1, tzinfo=UTC),
    call_id=None,
    actor="agent",
    action="call_started",
    resource_type="call",
    resource_id=None,
    outcome="success",
    reason=None,
    prev_mac="0" * 64,
)
ALL_SET = ChainRow(
    seq=42,
    ts=datetime(2026, 7, 1, 9, 5, 9, 500000, tzinfo=NEW_YORK),
    call_id="call-7f3a",
    actor="admin:ops.lead",
    action="call_ended",
    resource_type="call",
    resource_id="call-7f3a",
    outcome="failure",
    reason="caller_hung_up",
    prev_mac="ab" * 32,
)


def test_canonical_form_is_fixed() -> None:
    assert CANONICAL_VERSION == "sunrise-audit-v1"
    assert canonical_form(WITH_NULLS) == (
        b"16:sunrise-audit-v1"
        b"1:1"
        b"27:2026-11-24T15:30:00.000001Z"
        b"-"
        b"5:agent"
        b"12:call_started"
        b"4:call"
        b"-"
        b"7:success"
        b"-"
        b"64:" + b"0" * 64
    )
    assert canonical_form(ALL_SET) == (
        b"16:sunrise-audit-v1"
        b"2:42"
        b"27:2026-07-01T13:05:09.500000Z"
        b"9:call-7f3a"
        b"14:admin:ops.lead"
        b"10:call_ended"
        b"4:call"
        b"9:call-7f3a"
        b"7:failure"
        b"14:caller_hung_up"
        b"64:" + b"ab" * 32
    )
    with pytest.raises(ValueError, match="ts"):
        canonical_form(replace(WITH_NULLS, ts=datetime(2026, 11, 24, 15, 30)))


def decode(form: bytes) -> list[str | None]:
    """An independent reader of the documented form: `-` or `<length>:<bytes>` per field."""
    fields: list[str | None] = []
    at = 0
    while at < len(form):
        if form[at : at + 1] == b"-":
            fields.append(None)
            at += 1
            continue
        colon = form.index(b":", at)
        digits = form[at:colon]
        assert digits.isdigit() and (digits == b"0" or not digits.startswith(b"0")), form
        start = colon + 1
        end = start + int(digits)
        assert end <= len(form), form
        fields.append(form[start:end].decode("utf-8"))
        at = end
    return fields


def columns_of(row: ChainRow) -> list[str | None]:
    return [
        CANONICAL_VERSION,
        str(row.seq),
        row.ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        row.call_id,
        row.actor,
        row.action,
        row.resource_type,
        row.resource_id,
        row.outcome,
        row.reason,
        row.prev_mac,
    ]


def naive_join(row: ChainRow, separator: str) -> str:
    return separator.join(value or "" for value in columns_of(row))


def test_canonical_form_is_unambiguous() -> None:
    # Pairs of different events that a naive encoding would merge.
    pairs: list[tuple[dict[str, Any], dict[str, Any], str]] = [
        # A separator inside a field.
        ({"call_id": "a:b", "actor": "c"}, {"call_id": "a", "actor": "b:c"}, ":"),
        # An empty field and a null.
        ({"call_id": ""}, {"call_id": None}, ":"),
        ({"reason": ""}, {"reason": None}, "|"),
        # Fields shifted by one.
        (
            {"resource_type": "call", "resource_id": "1"},
            {"resource_type": "call1", "resource_id": None},
            "",
        ),
        (
            {"call_id": None, "actor": "agent", "action": "call_started"},
            {"call_id": "agent", "actor": "call_started", "action": ""},
            "",
        ),
        # A value that looks like an encoded field, and a null marker inside a value.
        ({"call_id": "5:agent", "actor": "x"}, {"call_id": "5:", "actor": "agentx"}, ""),
        ({"call_id": "-", "actor": "-"}, {"call_id": "--", "actor": ""}, ""),
    ]
    for first_change, second_change, separator in pairs:
        first = replace(WITH_NULLS, **first_change)
        second = replace(WITH_NULLS, **second_change)
        assert naive_join(first, separator) == naive_join(second, separator)

        assert canonical_form(first) != canonical_form(second), (first_change, second_change)
        assert decode(canonical_form(first)) == columns_of(first)
        assert decode(canonical_form(second)) == columns_of(second)

    # Every combination of awkward values: all forms distinct, each read back exactly.
    awkward = [None, "", "-", ":", "1:", "0", "a:b", "caf\u00e9", "-1:x"]
    present = [value for value in awkward if value is not None]
    forms = set()
    rows = [
        replace(WITH_NULLS, call_id=a, actor=b, resource_id=c, reason=d)
        for a, b, c, d in itertools.product(awkward, present, awkward, awkward)
    ]
    for row in rows:
        form = canonical_form(row)
        assert decode(form) == columns_of(row)
        forms.add(form)
    assert len(forms) == len(rows)
