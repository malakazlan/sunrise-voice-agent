import hashlib
import itertools
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import pytest
from psycopg import errors
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.engine.interfaces import ExecutionContext
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
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
from app.config import Settings
from app.db.session import create_database_engine, create_session_factory, create_sync_engine

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


def call_started(call_id: str = "call-1") -> Event:
    return Event(
        actor="agent",
        action=Action.CALL_STARTED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.SUCCESS,
        call_id=call_id,
        resource_id=call_id,
    )


def call_ended(call_id: str = "call-1") -> Event:
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
THREE = [call_started("call-1"), call_ended("call-1"), call_started("call-2")]


@pytest.fixture
def audit(database: Settings) -> AuditLog:
    return AuditLog(database.audit_hmac_key)


@pytest.fixture
async def factory(database: Settings) -> AsyncIterator[Callable[[], AsyncSession]]:
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


def broken(seq: int, kind: ChainBreak, checked: int) -> Verification:
    return Verification(ok=False, checked=checked, first_bad_seq=seq, kind=kind)


def intact(checked: int) -> Verification:
    return Verification(ok=True, checked=checked, first_bad_seq=None, kind=None)


async def lock_waiters(session: AsyncSession) -> int:
    count: int = (await session.execute(LOCK_WAITERS)).scalar_one()
    return count


async def wait_for_lock_waiters(session: AsyncSession, expected: int) -> None:
    with anyio.fail_after(10):
        while await lock_waiters(session) < expected:
            await session.rollback()
            await anyio.sleep(0.01)
    await session.rollback()


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
    audit: AuditLog, database: Settings, factory: Callable[[], AsyncSession]
) -> None:
    admin = create_sync_engine(database, isolation_level="AUTOCOMMIT", poolclass=NullPool)
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
    database: Settings,
    sync_engine: Engine,
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
            await append_all(audit, session, [call_started(f"call-{n}") for n in range(5)])
            assert await verified(audit, session) == intact(5)

            # Event 3 opens the second batch; its removal is seen across the batch boundary.
            tamper_with_audit_events(sync_engine, "DELETE FROM audit_events WHERE seq = 3")
            assert await verified(audit, session) == broken(4, ChainBreak.GAP, checked=2)
    finally:
        await engine.dispose()

    assert len(requested) == 2
    for options in requested:
        assert options["stream_results"] is True
        assert options["yield_per"] == 2


async def test_duplicate_seq_is_detected(
    audit: AuditLog, session: AsyncSession, sync_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    await append_all(audit, session, THREE)

    # Only possible after the owner drops the primary key: an exact copy of event 2.
    tamper_with_audit_events(
        sync_engine,
        "ALTER TABLE audit_events DROP CONSTRAINT pk_audit_events",
        "INSERT INTO audit_events SELECT * FROM audit_events WHERE seq = 2",
    )

    assert await verified(audit, session) == broken(2, ChainBreak.DUPLICATE, checked=2)


async def test_event_spliced_from_another_chain_is_detected(
    audit: AuditLog, session: AsyncSession, sync_engine: Engine, tamper_with_audit_events: Tamper
) -> None:
    # A second chain under the same key, for example a copy of the database, supplies an
    # event 2 whose own MAC is genuine but which follows a different event 1.
    await append_all(audit, session, [call_started("call-x"), call_ended("call-x")])
    foreign = (await stored(session))[1]
    tamper_with_audit_events(sync_engine, "TRUNCATE audit_events")

    await append_all(audit, session, THREE)
    tamper_with_audit_events(sync_engine, "DELETE FROM audit_events WHERE seq = 2")
    with sync_engine.begin() as connection:
        connection.execute(INSERT_ROW, dict(zip(ROW_COLUMNS, foreign, strict=True)))

    assert await verified(audit, session) == broken(2, ChainBreak.PREV_MAC_MISMATCH, checked=1)


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

# One edit per stored column, each made past the table's own protection, the seq at which
# verification must first fail and how many events it finds good before that. The seq edit
# renumbers the last event, so that the rows keep their order and only the MAC can tell.
EDITS = {
    "seq": ("UPDATE audit_events SET seq = 4 WHERE seq = 3", 4, 2),
    "ts": ("UPDATE audit_events SET ts = ts + interval '1 microsecond' WHERE seq = 2", 2, 1),
    "call_id": ("UPDATE audit_events SET call_id = 'call-9' WHERE seq = 2", 2, 1),
    "call_id_to_null": ("UPDATE audit_events SET call_id = NULL WHERE seq = 2", 2, 1),
    "actor": ("UPDATE audit_events SET actor = 'admin:ops' WHERE seq = 2", 2, 1),
    "action": ("UPDATE audit_events SET action = 'call_started' WHERE seq = 2", 2, 1),
    "resource_type": ("UPDATE audit_events SET resource_type = 'patient' WHERE seq = 2", 2, 1),
    "resource_id": ("UPDATE audit_events SET resource_id = 'call-9' WHERE seq = 2", 2, 1),
    "resource_id_to_null": ("UPDATE audit_events SET resource_id = NULL WHERE seq = 2", 2, 1),
    "outcome": ("UPDATE audit_events SET outcome = 'success' WHERE seq = 2", 2, 1),
    "reason": ("UPDATE audit_events SET reason = 'timeout' WHERE seq = 2", 2, 1),
    "reason_to_null": ("UPDATE audit_events SET reason = NULL WHERE seq = 2", 2, 1),
    "reason_from_null": ("UPDATE audit_events SET reason = 'caller_hung_up' WHERE seq = 1", 1, 0),
    "prev_mac": ("UPDATE audit_events SET prev_mac = repeat('0', 64) WHERE seq = 2", 2, 1),
    "prev_mac_not_ascii": (
        "UPDATE audit_events SET prev_mac = repeat(chr(233), 64) WHERE seq = 2",
        2,
        1,
    ),
    "mac": ("UPDATE audit_events SET mac = repeat('f', 64) WHERE seq = 2", 2, 1),
}


@pytest.mark.parametrize("column", EDITS)
async def test_edit_to_any_column_is_detected(
    audit: AuditLog,
    session: AsyncSession,
    sync_engine: Engine,
    tamper_with_audit_events: Tamper,
    column: str,
) -> None:
    statement, bad_seq, checked = EDITS[column]
    await append_all(audit, session, THREE)
    assert await verified(audit, session) == intact(3)
    before = await stored(session)

    tamper_with_audit_events(sync_engine, statement)

    assert await stored(session) != before
    assert await verified(audit, session) == broken(bad_seq, ChainBreak.MAC_MISMATCH, checked)


DELETIONS = {
    "middle": (["DELETE FROM audit_events WHERE seq = 2"], broken(3, ChainBreak.GAP, 1)),
    "first": (["DELETE FROM audit_events WHERE seq = 1"], broken(2, ChainBreak.WRONG_START, 0)),
    "two": (["DELETE FROM audit_events WHERE seq IN (2, 3)"], broken(4, ChainBreak.GAP, 1)),
}


@pytest.mark.parametrize("case", DELETIONS)
async def test_deleted_event_is_detected(
    audit: AuditLog,
    session: AsyncSession,
    sync_engine: Engine,
    tamper_with_audit_events: Tamper,
    case: str,
) -> None:
    statements, expected = DELETIONS[case]
    await append_all(audit, session, [*THREE, call_ended("call-2")])

    tamper_with_audit_events(sync_engine, *statements)

    assert await verified(audit, session) == expected


REORDERINGS = {
    "swap": (
        [
            "UPDATE audit_events SET seq = 0 WHERE seq = 2",
            "UPDATE audit_events SET seq = 2 WHERE seq = 3",
            "UPDATE audit_events SET seq = 3 WHERE seq = 0",
        ],
        broken(2, ChainBreak.MAC_MISMATCH, 1),
    ),
    "renumber_all": (
        ["UPDATE audit_events SET seq = seq + 10"],
        broken(11, ChainBreak.MAC_MISMATCH, 0),
    ),
    "close_a_gap": (
        [
            "DELETE FROM audit_events WHERE seq = 2",
            "UPDATE audit_events SET seq = 2 WHERE seq = 3",
            "UPDATE audit_events SET seq = 3 WHERE seq = 4",
        ],
        broken(2, ChainBreak.MAC_MISMATCH, 1),
    ),
}


@pytest.mark.parametrize("case", REORDERINGS)
async def test_reordered_events_are_detected(
    audit: AuditLog,
    session: AsyncSession,
    sync_engine: Engine,
    tamper_with_audit_events: Tamper,
    case: str,
) -> None:
    statements, expected = REORDERINGS[case]
    await append_all(audit, session, [*THREE, call_ended("call-2")])

    tamper_with_audit_events(sync_engine, *statements)

    assert await verified(audit, session) == expected


async def test_forged_event_is_detected(
    audit: AuditLog, session: AsyncSession, sync_engine: Engine
) -> None:
    await append_all(audit, session, THREE[:2])
    head_mac = (await stored(session))[-1][10]

    # Inserting needs no bypass. Without the key the forger can follow every other rule: the
    # next seq, the head's MAC as prev_mac, a hash of the documented input.
    forged = ChainRow(
        seq=3,
        ts=T0,
        call_id="call-1",
        actor="admin:intruder",
        action="call_ended",
        resource_type="call",
        resource_id="call-1",
        outcome="success",
        reason=None,
        prev_mac=head_mac,
    )
    digest = hashlib.sha256(head_mac.encode() + canonical_form(forged)).hexdigest()
    with sync_engine.begin() as connection:
        connection.execute(
            INSERT_ROW,
            {**{name: getattr(forged, name) for name in ROW_COLUMNS[:-1]}, "mac": digest},
        )

    # A genuine event written afterwards chains onto the forgery; the break is still found.
    assert await append_all(audit, session, [call_started("call-3")]) == [4]
    assert await verified(audit, session) == broken(3, ChainBreak.MAC_MISMATCH, checked=2)


async def test_wrong_key_fails_verification(audit: AuditLog, session: AsyncSession) -> None:
    await append_all(audit, session, THREE)
    other_key = "-".join(["other", "audit", "key"] * 4)
    other = AuditLog(SecretStr(other_key))

    assert await verified(other, session) == broken(1, ChainBreak.MAC_MISMATCH, checked=0)
    assert await verified(audit, session) == intact(3)
    assert other_key not in repr(other)
    with pytest.raises(ValueError, match="key"):
        AuditLog(SecretStr(""))


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
                    item = call_started(f"call-{number}-{index}")
                    seqs.append(await audit.append(session, item, now=T0))
                    await session.commit()
        except Exception as error:  # noqa: BLE001 (collected and asserted below)
            failures.append(type(error).__name__)
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


# --- R7, R8: the event and its action commit together -------------------------------------


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


def unwritable_event() -> Event:
    """An event the database refuses: free text forced past the API's own check."""
    item = call_started()
    object.__setattr__(item, "actor", "free text")
    return item


async def ignore_the_error_and_commit(audit: AuditLog, session: AsyncSession) -> None:
    await session.execute(INSERT_PATIENT)
    with pytest.raises(IntegrityError) as excinfo:
        await audit.append(session, unwritable_event(), now=T0)
    refusal = excinfo.value.orig
    assert isinstance(refusal, errors.CheckViolation)
    assert refusal.diag.constraint_name == "ck_audit_events_actor"
    await session.commit()


async def catch_it_in_a_savepoint(audit: AuditLog, session: AsyncSession) -> None:
    await session.execute(INSERT_PATIENT)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await audit.append(session, unwritable_event(), now=T0)
    await session.commit()


async def inside_a_transaction_block(audit: AuditLog, session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin():
            await session.execute(INSERT_PATIENT)
            await audit.append(session, unwritable_event(), now=T0)


CALLERS = {
    "ignore_the_error_and_commit": ignore_the_error_and_commit,
    "catch_it_in_a_savepoint": catch_it_in_a_savepoint,
    "inside_a_transaction_block": inside_a_transaction_block,
}


@pytest.mark.parametrize("caller", CALLERS)
async def test_action_fails_when_event_cannot_be_written(
    audit: AuditLog, session: AsyncSession, sync_engine: Engine, caller: str
) -> None:
    await CALLERS[caller](audit, session)

    with sync_engine.connect() as connection:
        assert connection.execute(PATIENTS).scalar_one() == 0
        assert connection.execute(COUNT).scalar_one() == 0


# --- R9, R10: the database's own rules ----------------------------------------------------


async def test_database_refuses_update_delete_truncate(
    audit: AuditLog, session: AsyncSession, sync_engine: Engine
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
        with pytest.raises(ProgrammingError) as excinfo, sync_engine.begin() as connection:
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
        longest = "Az09_-:." + "x" * 56
        assert getattr(changed(base, **{field: longest}), field) == longest
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
        replace(base, actor="admin:" + "x" * 59)
    with pytest.raises(TypeError, match="actor"):
        changed(base, actor=None)
    for actor in ("agent", "admin:ops", "admin:ops.lead-2:x_y", "admin:" + "x" * 58):
        assert replace(base, actor=actor).actor == actor


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


def test_database_rejects_free_text(sync_engine: Engine) -> None:
    seq = 0
    for column in TEXT_COLUMNS:
        for value in FREE_TEXT:
            seq += 1
            with pytest.raises(IntegrityError) as excinfo, sync_engine.begin() as connection:
                connection.execute(INSERT_ROW, {**valid_row(seq), column: value})
            refusal = excinfo.value.orig
            assert isinstance(refusal, errors.CheckViolation), (column, value)
            assert refusal.diag.constraint_name == f"ck_audit_events_{column}", (column, value)
            if len(value) > 2:
                assert value not in str(excinfo.value)

        # The longest allowed value, every allowed character, is accepted.
        seq += 1
        with sync_engine.begin() as connection:
            connection.execute(INSERT_ROW, {**valid_row(seq), column: "Az09_-:." + "x" * 56})

    with sync_engine.connect() as connection:
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

    # This slice's members, each a valid stored value.
    assert {member.value for member in Action} == {"call_started", "call_ended"}
    assert {member.value for member in ResourceType} == {"call"}
    assert {member.value for member in Outcome} == {"success", "failure"}
    assert {member.value for member in Reason} == {"caller_hung_up"}
    for registry in (Action, ResourceType, Outcome, Reason):
        for member in registry:
            assert re.fullmatch("[a-z0-9_]{1,64}", member.value)


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

# Computed with openssl, not with the code under test (Git Bash or any POSIX shell). E1 and E2
# are the two events' canonical forms; each MAC is one command, over prev_mac then the form:
#   K=fake-audit-key-fake-audit-key-fake-audit-key; Z=$(printf '%064d' 0); V=16:sunrise-audit-v1
#   E1="${V}1:127:2026-11-24T15:30:00.000001Z-5:agent12:call_started4:call-7:success-64:${Z}"
#   A=$(printf '%s' "$Z$E1" | openssl dgst -sha256 -hmac "$K" | cut -d' ' -f2)
#   E2="${V}1:227:2026-07-01T13:05:09.500000Z9:call-7f3a14:admin:ops.lead10:call_ended4:call"
#   E2="${E2}9:call-7f3a7:failure14:caller_hung_up64:${A}"
#   B=$(printf '%s' "$A$E2" | openssl dgst -sha256 -hmac "$K" | cut -d' ' -f2)
KNOWN_MAC_1 = "".join(
    "98091bc7 666c5dff 1b9b4e9f 073b8b66 621db182 cb347e1e f469dfb2 0c3f2b83".split()
)
KNOWN_MAC_2 = "".join(
    "991e079d 75a78ba9 f180eb7c 24873962 019a74f7 f008766b 6a06ebf1 9047f4a0".split()
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
        actor="admin:ops.lead",
        action=Action.CALL_ENDED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.FAILURE,
        call_id="call-7f3a",
        resource_id="call-7f3a",
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
