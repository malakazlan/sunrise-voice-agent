"""The admin console's reads, which the admin API serves (app/api/admin.py): the calls in
progress, one call's turns, the appointments, the slots of a window of days, and the summaries
of the guardrails and of the calls' numbers. They only read.

Calls in progress are derived from what is recorded, never asked of the voice process: a call
whose call_started event is no older than LIVE_WINDOW plus the longest a call may last, and that
has no call_ended event, with its latest turn's row. A call appears once its call_started event
is written and leaves when its call_ended event is; one whose process died mid-call stays until
the window has passed.

A summary covers whole days at the clinic, the last `days` of them, today included. Its
percentiles are Postgres's (percentile_cont, which interpolates), rounded to whole
milliseconds; a group with fewer than MIN_VALUES values of a stage gets its count and no
figure. A turn is a tool turn when it took a tool round, scripted when the model was not asked,
and plain otherwise. The rows the turn recorder dropped are read from the latest row it wrote.

An appointment names the call that booked it when that is known: a booking writes the
appointment and its request in one transaction, so both carry that transaction's created_at; an
appointment with exactly one call's booking request for its patient at that instant was booked
by that call. Seeded appointments have none.

Every value reaches the database as a bound parameter.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Integer, Numeric, and_, case, cast, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.audit.log import CALL_ID, Action
from app.audit.read import MAX_PAGE
from app.db.models import (
    Appointment,
    AuditEvent,
    AvailableSlot,
    CallTurn,
    Patient,
    Request,
    VerificationAttempt,
)
from app.domain.clinic import CLINIC_TZ

# How long after its start a call that is still going may last, beyond its configured longest.
LIVE_WINDOW = timedelta(minutes=15)
MAX_LIVE_CALLS = 100
# The most slots one read returns; the answer says when there were more.
MAX_SLOTS = 1000
# A percentile needs at least this many values.
MIN_VALUES = 20
STAGES = ("vad_ms", "stt_ms", "first_token_ms", "last_token_ms", "first_audio_ms", "total_ms")
KINDS = ("scripted", "plain", "tool")
GUARDRAIL_ACTIONS = (
    Action.GUARD_BLOCK,
    Action.EMERGENCY_DETECTED,
    Action.TOOL_REFUSED,
    Action.IDENTITY_LOCKED,
    Action.IDENTITY_REFUSED,
)
BOOKING = "booking"

TIMING_COLUMNS = tuple(getattr(CallTurn, stage) for stage in STAGES)


def clinic_midnight(day: date) -> datetime:
    """The first instant of a day at the clinic."""
    return datetime.combine(day, time(), tzinfo=CLINIC_TZ)


def window_start(now: datetime, days: int) -> datetime:
    """The first instant of the last `days` days at the clinic, today included."""
    if now.utcoffset() is None:
        raise ValueError("now must carry a time zone")
    return clinic_midnight(now.astimezone(CLINIC_TZ).date() - timedelta(days=days - 1))


def _clinic_day(instant: Any) -> ColumnElement[datetime]:
    """The first instant of the clinic's day that holds `instant`."""
    local = func.timezone(CLINIC_TZ.key, instant)
    return func.timezone(CLINIC_TZ.key, func.date_trunc("day", local))


def _in_clinic_time(row: dict[str, Any], *names: str) -> dict[str, Any]:
    return {**row, **{name: row[name].astimezone(CLINIC_TZ) for name in names}}


async def live_calls(
    session: AsyncSession, *, now: datetime, max_call_seconds: int
) -> list[dict[str, Any]]:
    """The calls in progress, newest first, each with its latest turn's facts and timings (none
    before its first turn)."""
    since = now - LIVE_WINDOW - timedelta(seconds=max_call_seconds)
    started = aliased(AuditEvent)
    ended = aliased(AuditEvent)
    latest = (
        select(
            CallTurn.turn,
            CallTurn.phase,
            CallTurn.verified,
            CallTurn.patient_id,
            CallTurn.last_tool,
            *TIMING_COLUMNS,
        )
        .where(CallTurn.call_id == started.call_id)
        .order_by(CallTurn.turn.desc())
        .limit(1)
        .lateral()
    )
    has_ended = (
        select(ended.seq)
        .where(
            ended.action == Action.CALL_ENDED.value,
            ended.ts >= since,
            ended.call_id == started.call_id,
        )
        .exists()
    )
    statement = (
        select(started.call_id, started.ts.label("started_at"), *latest.c)
        .select_from(started)
        .outerjoin(latest, true())
        .where(started.action == Action.CALL_STARTED.value, started.ts >= since, ~has_ended)
        .order_by(started.ts.desc(), started.seq.desc())
        .limit(MAX_LIVE_CALLS)
    )
    return [dict(row._mapping) for row in await session.execute(statement)]


async def call_turns(
    session: AsyncSession, call_id: str, *, limit: int, after: int | None
) -> list[dict[str, Any]]:
    """At most `limit` of the call's turns after turn `after`, in order."""
    if not CALL_ID.fullmatch(call_id):
        raise ValueError("call_id does not have the shape of a call id")
    if not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit must be 1 to {MAX_PAGE}")
    statement = select(
        CallTurn.turn,
        CallTurn.at,
        CallTurn.phase,
        CallTurn.verified,
        CallTurn.patient_id,
        CallTurn.guard_action,
        CallTurn.last_tool,
        CallTurn.tool_rounds,
        *TIMING_COLUMNS,
        CallTurn.record_us,
    ).where(CallTurn.call_id == call_id)
    if after is not None:
        statement = statement.where(CallTurn.turn > after)
    rows = await session.execute(statement.order_by(CallTurn.turn).limit(limit))
    return [dict(row._mapping) for row in rows]


async def appointments(
    session: AsyncSession, *, limit: int, before: int | None
) -> list[dict[str, Any]]:
    """At most `limit` appointments with an id below `before`, newest first: when (at the
    clinic and in UTC), with whom, for which patient and whether new, why, their status and the
    call that booked them when known."""
    if not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit must be 1 to {MAX_PAGE}")
    booked = (
        select(
            Request.patient_id,
            Request.created_at,
            func.min(Request.call_id).label("call_id"),
        )
        .where(Request.type == BOOKING)
        .group_by(Request.patient_id, Request.created_at)
        .having(func.count(func.distinct(Request.call_id)) == 1)
        .subquery()
    )
    statement = (
        select(
            Appointment.id,
            Appointment.starts_at,
            Appointment.starts_at.label("starts_at_utc"),
            Appointment.doctor,
            Appointment.patient_id,
            Patient.is_new.label("new_patient"),
            Appointment.reason_short.label("reason"),
            Appointment.status,
            booked.c.call_id.label("booked_by_call"),
        )
        .join(Patient, Patient.id == Appointment.patient_id)
        .outerjoin(
            booked,
            and_(
                booked.c.patient_id == Appointment.patient_id,
                booked.c.created_at == Appointment.created_at,
            ),
        )
    )
    if before is not None:
        statement = statement.where(Appointment.id < before)
    rows = await session.execute(statement.order_by(Appointment.id.desc()).limit(limit))
    return [
        {
            **_in_clinic_time(dict(row._mapping), "starts_at"),
            "starts_at_utc": row.starts_at_utc.astimezone(UTC),
        }
        for row in rows
    ]


async def slots(
    session: AsyncSession, *, start: date, days: int
) -> tuple[list[dict[str, Any]], bool]:
    """The slots of `days` days at the clinic from `start`, in order, at most MAX_SLOTS of
    them; and whether there were more."""
    statement = (
        select(
            AvailableSlot.id,
            AvailableSlot.doctor,
            AvailableSlot.starts_at,
            AvailableSlot.is_available.label("available"),
        )
        .where(
            AvailableSlot.starts_at >= clinic_midnight(start),
            AvailableSlot.starts_at < clinic_midnight(start + timedelta(days=days)),
        )
        .order_by(AvailableSlot.starts_at, AvailableSlot.id)
        .limit(MAX_SLOTS + 1)
    )
    rows = [
        _in_clinic_time(dict(row._mapping), "starts_at") for row in await session.execute(statement)
    ]
    return rows[:MAX_SLOTS], len(rows) > MAX_SLOTS


async def guardrail_counts(session: AsyncSession, *, since: datetime) -> list[dict[str, Any]]:
    """For each clinic day from `since`: the guardrail events of each action and reason."""
    events = (
        select(_clinic_day(AuditEvent.ts).label("day"), AuditEvent.action, AuditEvent.reason)
        .where(
            AuditEvent.action.in_([action.value for action in GUARDRAIL_ACTIONS]),
            AuditEvent.ts >= since,
        )
        .subquery()
    )
    statement = (
        select(events.c.day, events.c.action, events.c.reason, func.count().label("count"))
        .group_by(events.c.day, events.c.action, events.c.reason)
        .order_by(events.c.day, events.c.action, events.c.reason)
    )
    return [_in_clinic_time(dict(row._mapping), "day") for row in await session.execute(statement)]


def _percentile(column: Any, fraction: float) -> ColumnElement[int | None]:
    """The column's percentile, rounded to a whole number; none with fewer than MIN_VALUES
    values."""
    figure = func.percentile_cont(fraction).within_group(column)
    rounded = cast(func.round(cast(figure, Numeric)), Integer)
    return case((func.count(column) >= MIN_VALUES, rounded), else_=None)


async def _calls_per_day(session: AsyncSession, since: datetime) -> list[dict[str, Any]]:
    started = (
        select(_clinic_day(AuditEvent.ts).label("day"))
        .where(AuditEvent.action == Action.CALL_STARTED.value, AuditEvent.ts >= since)
        .subquery()
    )
    statement = (
        select(started.c.day, func.count().label("calls"))
        .group_by(started.c.day)
        .order_by(started.c.day)
    )
    return [_in_clinic_time(dict(row._mapping), "day") for row in await session.execute(statement)]


async def _latency(session: AsyncSession, since: datetime) -> list[dict[str, Any]]:
    kind = case(
        (CallTurn.tool_rounds > 0, KINDS[2]),
        (CallTurn.first_token_ms.is_(None), KINDS[0]),
        else_=KINDS[1],
    )
    turns = select(kind.label("kind"), *TIMING_COLUMNS).where(CallTurn.at >= since).subquery()
    figures = [
        figure
        for stage in STAGES
        for figure in (
            func.count(turns.c[stage]).label(f"{stage}_values"),
            _percentile(turns.c[stage], 0.5).label(f"{stage}_p50"),
            _percentile(turns.c[stage], 0.95).label(f"{stage}_p95"),
        )
    ]
    statement = select(turns.c.kind, func.count().label("turns"), *figures).group_by(turns.c.kind)
    found = {row.kind: row._mapping for row in await session.execute(statement)}
    shown = []
    for each in KINDS:
        row = found.get(each)
        for stage in STAGES:
            shown += [
                {
                    "kind": each,
                    "turns": 0 if row is None else row["turns"],
                    "stage": stage,
                    "values": 0 if row is None else row[f"{stage}_values"],
                    "p50": None if row is None else row[f"{stage}_p50"],
                    "p95": None if row is None else row[f"{stage}_p95"],
                }
            ]
    return shown


async def metrics(session: AsyncSession, *, since: datetime) -> dict[str, Any]:
    """From `since`: the calls started each clinic day, the calls ended by reason, the calls
    verified, the verification attempts and their successes, the per-stage percentiles of each
    kind of turn, and the median cost of recording a turn; and the rows the turn recorder has
    dropped since its process started, as of the latest row it wrote."""
    ended = await session.execute(
        select(AuditEvent.reason, func.count().label("calls"))
        .where(AuditEvent.action == Action.CALL_ENDED.value, AuditEvent.ts >= since)
        .group_by(AuditEvent.reason)
        .order_by(AuditEvent.reason)
    )
    verified_calls = await session.scalar(
        select(func.count(func.distinct(AuditEvent.call_id))).where(
            AuditEvent.action == Action.IDENTITY_VERIFIED.value, AuditEvent.ts >= since
        )
    )
    attempts = (
        await session.execute(
            select(
                func.count().label("verification_attempts"),
                func.count().filter(VerificationAttempt.success).label("verification_successes"),
            ).where(VerificationAttempt.created_at >= since)
        )
    ).one()
    recording = (
        await session.execute(
            select(
                func.count().label("record_us_turns"),
                _percentile(CallTurn.record_us, 0.5).label("record_us_p50"),
            ).where(CallTurn.at >= since)
        )
    ).one()
    dropped = await session.scalar(
        select(CallTurn.dropped_before).order_by(CallTurn.id.desc()).limit(1)
    )
    return {
        "calls_per_day": await _calls_per_day(session, since),
        "ended": [dict(row._mapping) for row in ended],
        "verified_calls": verified_calls,
        **attempts._mapping,
        "latency": await _latency(session, since),
        **recording._mapping,
        "turn_rows_dropped": 0 if dropped is None else dropped,
    }
