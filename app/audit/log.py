"""The audit log: who did what to which record, when, and how it ended.

This module is the only code that writes or verifies `audit_events` (a repo hygiene test, a lint
against accidents, keeps it that way). Each event is chained to the one before it with a keyed
MAC, so that changing, removing, reordering or forging an event breaks the chain where it
happened.

MAC
    HMAC-SHA256 under the audit key over `prev_mac` (the 64 lowercase hex characters stored
    with the event, as ASCII) followed by the event's canonical form. Stored as lowercase hex.
    The first event's `prev_mac` is 64 zeros.

Canonical form (version `sunrise-audit-v1`)
    Eleven fields in this order: the version text, then every column except `mac`:

        version, seq, ts, call_id, actor, action, resource_type, resource_id, outcome,
        reason, prev_mac

    A null is the single byte `-`. Any other value is its UTF-8 byte length in decimal (no
    sign, no leading zero), a `:`, then its UTF-8 bytes; for example `5:agent`, and `0:` for
    an empty value. `seq` is written in decimal. `ts` is the instant in UTC to the
    microsecond: `YYYY-MM-DDTHH:MM:SS.ffffffZ`, always with six digits after the point.

    The form is unambiguous by construction: a field starts either with `-` or with a digit,
    so a null and an empty value differ, and a value's length says exactly where it ends, so
    no byte inside a value (`:`, `-`, digits) can be read as a boundary. The field count and
    order are fixed. Two different events therefore never share a canonical form, and since
    `prev_mac` is always 64 ASCII bytes, never share a MAC input either. A change to any of
    this is a new version, and the version text is part of every MAC.

Identifiers
    Every identifier has a declared shape:

    - `call_id`, and the id of a `call`: a version-4 UUID as 32 lowercase hex digits, its
      version digit 4 and its variant 8, 9, a or b checked, as app/voice/calls.py gives each
      call (`uuid.uuid4().hex`).
    - the id of a `patient`: `P` and 3 to 6 digits, as the seed data has them (`P001`).
    - the id of an `appointment`, `medication`, `slot`, `request`, `message` or
      `audit_event`: the row's number (an event's seq), 1 to 8 digits, no leading zero.
    - the id of a `tool`: its registered name, 2 to 40 lower-case letters and underscores, as
      app/agent/tool_registry.py declares tools. A name the model gives that is not a
      registered tool is never recorded.
    - `actor`: `agent`, or `admin:` and a user name of 1 to 32 letters and underscores,
      starting with a letter (ADMIN_NAME, which ADMIN_USER is held to as well).

    The shapes limit what an identifier can look like, not what it can mean: a patient id, a
    row id and the digits inside a call id can coincide with other numbers, such as part of a
    date, a phone number or an SSN. The control for `call_id` is where it comes from: the
    server generates it and never takes one from the caller or the model. A resource type
    without a declared id shape cannot be used. The database's CHECKs (letters, digits,
    `_ - : .`, at most 64) stay as the outer bound.

Concurrency and the caller's transaction
    `append` runs at the top level of the caller's transaction, never in a nested one, and
    the caller uses the default isolation level, READ COMMITTED. It waits at most
    LOCK_TIMEOUT_MS for the chain's transaction-scoped advisory lock, then reads the head; the
    lock ends with the caller's transaction, committed, rolled back or killed, so callers
    append last, just before they commit. The database refuses a row out of chain position on
    its own (see the position trigger in app/db/models.py). Under REPEATABLE READ or
    SERIALIZABLE a contended append sees a stale head and fails on the primary key: it fails
    closed. Any failure, cancellation included, rolls back the caller's whole transaction.
"""

import asyncio
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import SecretStr
from sqlalchemy import BigInteger, and_, case, cast, extract, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ADMIN_NAME
from app.db.models import AuditEvent
from app.db.session import in_database_transaction

logger = logging.getLogger(__name__)

CANONICAL_VERSION = "sunrise-audit-v1"
ZERO_MAC = "0" * 64
# Rows fetched from the server per round trip while verifying; memory stays at one batch.
VERIFY_BATCH_SIZE = 1000
# verify lists at most this many breaks and says when there were more.
MAX_REPORTED_BREAKS = 100
# How long append waits for the chain's lock before it fails, like any other failure.
LOCK_TIMEOUT_MS = 5000
# The advisory lock that serialises appends: a fixed number, the bytes of "audit".
CHAIN_LOCK = int.from_bytes(b"audit", "big")

AGENT = "agent"
ADMIN_PREFIX = "admin:"
CALL_ID = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")
PATIENT_ID = re.compile(r"P[0-9]{3,6}")
ROW_ID = re.compile(r"[1-9][0-9]{0,7}")
TOOL_NAME = re.compile(r"[a-z_]{2,40}")

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Action(StrEnum):
    CALL_STARTED = "call_started"
    CALL_ENDED = "call_ended"
    TOOL_CALLED = "tool_called"
    TOOL_REFUSED = "tool_refused"
    TOOL_FAILED = "tool_failed"
    IDENTITY_CLAIMED = "identity_claimed"
    IDENTITY_VERIFIED = "identity_verified"
    IDENTITY_REFUSED = "identity_refused"
    IDENTITY_LOCKED = "identity_locked"
    APPOINTMENT_READ = "appointment_read"
    REFILL_REQUESTED = "refill_requested"
    LAB_STATUS_READ = "lab_status_read"
    BALANCE_READ = "balance_read"
    DOCTOR_MESSAGE_SENT = "doctor_message_sent"
    ADMIN_READ_REQUESTS = "admin_read_requests"
    ADMIN_READ_MESSAGES = "admin_read_messages"
    ADMIN_READ_AUDIT = "admin_read_audit"
    ADMIN_VERIFY_AUDIT = "admin_verify_audit"
    REFILL_PREVIEWED = "refill_previewed"
    REFILL_NOT_FOUND = "refill_not_found"
    DOCTOR_MESSAGE_PREVIEWED = "doctor_message_previewed"
    MESSAGE_TAKEN = "message_taken"
    MESSAGE_PREVIEWED = "message_previewed"
    TRANSFER_REQUESTED = "transfer_requested"
    TRANSFER_PREVIEWED = "transfer_previewed"
    EMERGENCY_DETECTED = "emergency_detected"
    CALL_CLOSED = "call_closed"
    NEW_PATIENT_STARTED = "new_patient_started"
    NEW_PATIENT_DETAILS_COLLECTED = "new_patient_details_collected"
    SLOTS_OFFERED = "slots_offered"
    APPOINTMENT_BOOKED = "appointment_booked"
    NEW_PATIENT_BOOKED = "new_patient_booked"
    BOOKING_PREVIEWED = "booking_previewed"
    SLOT_TAKEN = "slot_taken"
    BOOKING_REPEATED = "booking_repeated"
    GUARD_BLOCK = "guard_block"


class ResourceType(StrEnum):
    CALL = "call"
    PATIENT = "patient"
    APPOINTMENT = "appointment"
    MEDICATION = "medication"
    SLOT = "slot"
    TOOL = "tool"
    REQUEST = "request"
    MESSAGE = "message"
    AUDIT_EVENT = "audit_event"


# The shape of each resource type's ids.
ID_PATTERNS: dict[ResourceType, re.Pattern[str]] = {
    ResourceType.CALL: CALL_ID,
    ResourceType.PATIENT: PATIENT_ID,
    ResourceType.APPOINTMENT: ROW_ID,
    ResourceType.MEDICATION: ROW_ID,
    ResourceType.SLOT: ROW_ID,
    ResourceType.TOOL: TOOL_NAME,
    ResourceType.REQUEST: ROW_ID,
    ResourceType.MESSAGE: ROW_ID,
    ResourceType.AUDIT_EVENT: ROW_ID,
}


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    REFUSED = "refused"


class Reason(StrEnum):
    # Why a call ended.
    CALLER_HUNG_UP = "caller_hung_up"
    COMPLETED = "completed"
    EMERGENCY = "emergency"
    TRANSFERRED = "transferred"
    IDLE_TIMEOUT = "idle_timeout"
    MAX_DURATION = "max_duration"
    ERROR = "error"
    # What detected an emergency.
    GUARD = "guard"
    MODEL = "model"
    # Why a call was transferred to the staff.
    CALLER_REQUEST = "caller_request"
    FRUSTRATION = "frustration"
    VERIFICATION_LOCKED = "verification_locked"
    OTHER = "other"
    # The output guard's rules, each the reason of a guard_block (app/guard/output.py).
    SSN_LAST4 = "ssn_last4"
    SSN_SHAPE = "ssn_shape"
    PHI_OUTSIDE_GATE = "phi_outside_gate"
    MEDICAL_ADVICE = "medical_advice"
    REFILL_PROMISE = "refill_promise"


def _check_member(name: str, value: object, registry: type[StrEnum]) -> None:
    if not isinstance(value, registry):
        raise TypeError(f"{name} must be a member of {registry.__name__}")


def _check_id(name: str, value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} does not have the declared shape of its kind of id")
    return value


def _check_actor(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("actor must be text")
    if value == AGENT:
        return
    if not (value.startswith(ADMIN_PREFIX) and ADMIN_NAME.fullmatch(value[len(ADMIN_PREFIX) :])):
        raise ValueError("actor must be agent, or admin: followed by a user name")


def _validate(event: "Event") -> None:
    """Every rule an event must meet. An error names the field, never the value."""
    _check_actor(event.actor)
    _check_member("action", event.action, Action)
    _check_member("resource_type", event.resource_type, ResourceType)
    _check_member("outcome", event.outcome, Outcome)
    if event.reason is not None:
        _check_member("reason", event.reason, Reason)
    if event.call_id is not None:
        _check_id("call_id", event.call_id, CALL_ID)
    pattern = ID_PATTERNS.get(event.resource_type)
    if pattern is None:
        raise ValueError("resource_type has no declared shape for its ids")
    if event.resource_id is not None:
        _check_id("resource_id", event.resource_id, pattern)


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """What happened, built from registry members and identifiers only. Checked when it is
    made and again when it is written.
    """

    actor: str
    action: Action
    resource_type: ResourceType
    outcome: Outcome
    call_id: str | None = None
    resource_id: str | None = None
    reason: Reason | None = None

    def __post_init__(self) -> None:
        _validate(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChainRow:
    """Every stored column of an event except `mac`: what the MAC covers."""

    seq: int
    ts: datetime
    call_id: str | None
    actor: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    reason: str | None
    prev_mac: str


class ChainBreak(StrEnum):
    MAC_MISMATCH = "mac_mismatch"
    PREV_MAC_MISMATCH = "prev_mac_mismatch"
    GAP = "gap"
    DUPLICATE = "duplicate"
    WRONG_START = "wrong_start"
    # A stored value that cannot be read or put in canonical form, such as ts = infinity.
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True, kw_only=True)
class Break:
    """`seq` is None for a row whose seq cannot be read."""

    seq: int | None
    kind: ChainBreak


@dataclass(frozen=True, slots=True, kw_only=True)
class Verification:
    """`checked` counts every event read. `breaks` lists every break in seq order, at most
    MAX_REPORTED_BREAKS of them; `truncated` says more were found. `ok` only with none.
    """

    ok: bool
    checked: int
    breaks: tuple[Break, ...] = ()
    truncated: bool = False


def _field(value: str | None) -> bytes:
    if value is None:
        return b"-"
    data = value.encode("utf-8")
    return str(len(data)).encode("ascii") + b":" + data


def _utc_text(ts: datetime) -> str:
    if ts.utcoffset() is None:
        raise ValueError("ts must carry a time zone")
    utc = ts.astimezone(UTC)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond:06d}Z"
    )


def canonical_form(row: ChainRow) -> bytes:
    """The documented encoding of an event (see the module docstring)."""
    return b"".join(
        _field(value)
        for value in (
            CANONICAL_VERSION,
            str(row.seq),
            _utc_text(row.ts),
            row.call_id,
            row.actor,
            row.action,
            row.resource_type,
            row.resource_id,
            row.outcome,
            row.reason,
            row.prev_mac,
        )
    )


async def _chain_head(session: AsyncSession) -> tuple[int, str] | None:
    """The last event's seq and MAC, or None for an empty log."""
    head = (
        await session.execute(
            select(AuditEvent.seq, AuditEvent.mac).order_by(AuditEvent.seq.desc()).limit(1)
        )
    ).one_or_none()
    return None if head is None else (head.seq, head.mac)


async def _roll_back_to_the_end(session: AsyncSession) -> None:
    """Roll the session back, and finish rolling back even if the task is cancelled while it
    runs: the rollback runs as its own task and is waited for until it is done.
    """
    rollback = asyncio.ensure_future(session.rollback())
    while not rollback.done():
        try:
            await asyncio.shield(rollback)
        except asyncio.CancelledError:
            continue
    rollback.result()


# The stored instant as microseconds since 1970 when Python can hold it (years 1 to 9999),
# otherwise null: infinity or a year the driver cannot represent never fails the read.
_TS_MICROSECONDS = case(
    (
        and_(
            func.isfinite(AuditEvent.ts),
            extract("year", func.timezone("UTC", AuditEvent.ts)).between(1, 9999),
        ),
        cast(extract("epoch", AuditEvent.ts) * 1_000_000, BigInteger),
    ),
    else_=None,
)


class AuditLog:
    """Appends to and verifies the chain under one key. Build it once from
    `Settings.audit_hmac_key` and share it; it holds nothing else.
    """

    __slots__ = ("_key",)

    def __init__(self, key: SecretStr) -> None:
        secret = key.get_secret_value()
        if not secret:
            raise ValueError("the audit key must not be empty")
        self._key = secret.encode("utf-8")

    def _mac(self, row: ChainRow) -> str:
        message = row.prev_mac.encode("ascii") + canonical_form(row)
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    async def append(self, session: AsyncSession, event: Event, *, now: datetime) -> int:
        """Add `event` at the instant `now` inside the caller's open transaction and return
        its seq. Does not commit. Any failure, from a refused argument to cancellation, rolls
        back the caller's whole transaction before the original error is raised, so the
        action it records can never commit without it, however the caller handles the error.
        A session in autocommit mode or in a nested transaction is refused. The lock timeout
        it sets stays in force for the rest of the caller's transaction and replaces any the
        caller set; callers append last.
        """
        try:
            if type(event) is not Event:
                raise TypeError("append takes an Event")
            _validate(event)
            if not isinstance(now, datetime):
                raise TypeError("now must be a datetime")
            if now.utcoffset() is None:
                raise ValueError("now must carry a time zone")
            if session.in_nested_transaction():
                raise RuntimeError(
                    "append runs at the top level of the caller's transaction, not in a nested"
                    " transaction"
                )
            await session.execute(
                select(func.set_config("lock_timeout", f"{LOCK_TIMEOUT_MS}ms", True))
            )
            await session.execute(select(func.pg_advisory_xact_lock(CHAIN_LOCK)))
            if not await in_database_transaction(session):
                raise RuntimeError(
                    "append needs a database transaction to roll back; the session is in"
                    " autocommit mode"
                )
            head = await _chain_head(session)
            seq, prev_mac = (1, ZERO_MAC) if head is None else (head[0] + 1, head[1])
            row = ChainRow(
                seq=seq,
                ts=now.astimezone(UTC),
                call_id=event.call_id,
                actor=event.actor,
                action=event.action.value,
                resource_type=event.resource_type.value,
                resource_id=event.resource_id,
                outcome=event.outcome.value,
                reason=None if event.reason is None else event.reason.value,
                prev_mac=prev_mac,
            )
            mac = self._mac(row)
            written = await session.execute(
                insert(AuditEvent)
                .values(
                    seq=row.seq,
                    ts=row.ts,
                    call_id=row.call_id,
                    actor=row.actor,
                    action=row.action,
                    resource_type=row.resource_type,
                    resource_id=row.resource_id,
                    outcome=row.outcome,
                    reason=row.reason,
                    prev_mac=row.prev_mac,
                    mac=mac,
                )
                .returning(AuditEvent.seq)
            )
            # A rule or trigger can make an INSERT store nothing without an error.
            if written.scalars().all() != [seq]:
                raise RuntimeError("the audit event was not stored: the INSERT returned no row")
            # A rule can send the row elsewhere and still return it, and a trigger can change
            # it on the way in: read the whole row back and compare it with what was written.
            stored = (
                await session.execute(
                    select(
                        AuditEvent.seq,
                        AuditEvent.ts,
                        AuditEvent.call_id,
                        AuditEvent.actor,
                        AuditEvent.action,
                        AuditEvent.resource_type,
                        AuditEvent.resource_id,
                        AuditEvent.outcome,
                        AuditEvent.reason,
                        AuditEvent.prev_mac,
                        AuditEvent.mac,
                    ).where(AuditEvent.seq == seq)
                )
            ).all()
            if not stored:
                raise RuntimeError("the audit event was not stored: it is not in audit_events")
            written_row = (
                row.seq,
                row.ts,
                row.call_id,
                row.actor,
                row.action,
                row.resource_type,
                row.resource_id,
                row.outcome,
                row.reason,
                row.prev_mac,
                mac,
            )
            if [tuple(found) for found in stored] != [written_row]:
                raise RuntimeError(
                    "the audit event was not stored as written: the stored row differs from"
                    " what was written"
                )
        except BaseException:
            try:
                await _roll_back_to_the_end(session)
            except Exception as failure:
                logger.warning("audit_append_rollback_failed error=%s", type(failure).__name__)
            raise
        return seq

    def _is_authentic(self, row: ChainRow, mac: str) -> bool:
        try:
            expected = self._mac(row)
        except UnicodeEncodeError:
            # A prev_mac that is not ASCII was never written by append.
            return False
        return hmac.compare_digest(expected.encode("ascii"), mac.encode("utf-8"))

    async def verify(self, session: AsyncSession) -> Verification:
        """Walk the whole chain in seq order, VERIFY_BATCH_SIZE rows at a time, and report
        every break. After a break the walk goes on from the broken row's stored MAC and seq,
        so later tampering is still found. Never raises because of what is stored. Reads
        only; the caller ends the transaction.
        """
        statement = (
            select(
                AuditEvent.seq,
                _TS_MICROSECONDS.label("ts_microseconds"),
                AuditEvent.call_id,
                AuditEvent.actor,
                AuditEvent.action,
                AuditEvent.resource_type,
                AuditEvent.resource_id,
                AuditEvent.outcome,
                AuditEvent.reason,
                AuditEvent.prev_mac,
                AuditEvent.mac,
            )
            .order_by(AuditEvent.seq)
            .execution_options(yield_per=VERIFY_BATCH_SIZE)
        )
        checked = 0
        breaks: list[Break] = []
        truncated = False
        anchor: str = ZERO_MAC
        expected_seq = 1
        started = False
        result = await session.stream(statement)
        try:
            async for batch in result.partitions():
                for stored in batch:
                    seq = stored.seq if isinstance(stored.seq, int) else None
                    if seq is None:
                        # No seq to report and no position to move on from.
                        kinds = [ChainBreak.UNREADABLE]
                    else:
                        kinds = self._breaks_at(stored, expected_seq, anchor, first=not started)
                        anchor = stored.mac
                        expected_seq = seq + 1
                        started = True
                    for kind in kinds:
                        if len(breaks) < MAX_REPORTED_BREAKS:
                            breaks.append(Break(seq=seq, kind=kind))
                        else:
                            truncated = True
                    checked += 1
        finally:
            await result.close()
        return Verification(
            ok=not breaks, checked=checked, breaks=tuple(breaks), truncated=truncated
        )

    def _breaks_at(
        self, stored: Any, expected_seq: int, anchor: str, *, first: bool
    ) -> list[ChainBreak]:
        """Every rule a stored row with a readable seq breaks, given the seq and the previous
        MAC that should come next. Its content (MAC) and its position (seq) are checked
        whatever the other says, so a row can break both; its link to the previous MAC only
        when both are good, since an edited or misplaced row has no meaningful link.
        """
        kinds = []
        authentic = False
        try:
            if stored.ts_microseconds is None:
                kinds.append(ChainBreak.UNREADABLE)
            else:
                row = ChainRow(
                    seq=stored.seq,
                    ts=EPOCH + timedelta(microseconds=stored.ts_microseconds),
                    call_id=stored.call_id,
                    actor=stored.actor,
                    action=stored.action,
                    resource_type=stored.resource_type,
                    resource_id=stored.resource_id,
                    outcome=stored.outcome,
                    reason=stored.reason,
                    prev_mac=stored.prev_mac,
                )
                authentic = self._is_authentic(row, stored.mac)
                if not authentic:
                    kinds.append(ChainBreak.MAC_MISMATCH)
        except Exception:
            kinds.append(ChainBreak.UNREADABLE)
        in_position = True
        if first and stored.seq != 1:
            kinds.append(ChainBreak.WRONG_START)
            in_position = False
        elif not first and stored.seq < expected_seq:
            kinds.append(ChainBreak.DUPLICATE)
            in_position = False
        elif not first and stored.seq > expected_seq:
            kinds.append(ChainBreak.GAP)
            in_position = False
        if authentic and in_position:
            if first and stored.prev_mac != ZERO_MAC:
                kinds.append(ChainBreak.WRONG_START)
            elif not first and stored.prev_mac != anchor:
                kinds.append(ChainBreak.PREV_MAC_MISMATCH)
        return kinds
