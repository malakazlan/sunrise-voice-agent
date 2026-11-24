"""The audit log: who did what to which record, when, and how it ended.

This module is the only code that writes or verifies `audit_events` (a repo hygiene test keeps
it that way). Each event is chained to the one before it with a keyed MAC, so that changing,
removing, reordering or forging an event breaks the chain where it happened.

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

Concurrency
    `append` takes a transaction-scoped advisory lock before it reads the chain head, so
    appends are serialised and the lock ends with the caller's transaction, committed, rolled
    back or killed. Other writers wait for it, so callers append last, just before they commit.
    The primary key on `seq` refuses a duplicate even if the lock were bypassed.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import SecretStr
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AUDIT_TEXT_PATTERN, AuditEvent
from app.db.session import in_database_transaction

CANONICAL_VERSION = "sunrise-audit-v1"
ZERO_MAC = "0" * 64
# The advisory lock that serialises appends: a fixed number, the bytes of "audit".
CHAIN_LOCK = int.from_bytes(b"audit", "big")
# Rows fetched from the server per round trip while verifying; memory stays at one batch.
VERIFY_BATCH_SIZE = 1000

IDENTIFIER = re.compile(AUDIT_TEXT_PATTERN)
AGENT = "agent"
ADMIN_PREFIX = "admin:"


class Action(StrEnum):
    CALL_STARTED = "call_started"
    CALL_ENDED = "call_ended"


class ResourceType(StrEnum):
    CALL = "call"


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class Reason(StrEnum):
    CALLER_HUNG_UP = "caller_hung_up"


def _check_member(name: str, value: object, registry: type[StrEnum]) -> None:
    if not isinstance(value, registry):
        raise TypeError(f"{name} must be a member of {registry.__name__}")


def _check_identifier(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be 1 to 64 letters, digits, _ - : or .")
    return value


def _check_actor(value: object) -> None:
    actor = _check_identifier("actor", value)
    if actor != AGENT and not (actor.startswith(ADMIN_PREFIX) and len(actor) > len(ADMIN_PREFIX)):
        raise ValueError("actor must be agent, or admin: followed by an identifier")


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """What happened, built from registry members and identifiers only. Values are checked
    here; an error names the field, never the value.
    """

    actor: str
    action: Action
    resource_type: ResourceType
    outcome: Outcome
    call_id: str | None = None
    resource_id: str | None = None
    reason: Reason | None = None

    def __post_init__(self) -> None:
        _check_actor(self.actor)
        _check_member("action", self.action, Action)
        _check_member("resource_type", self.resource_type, ResourceType)
        _check_member("outcome", self.outcome, Outcome)
        if self.reason is not None:
            _check_member("reason", self.reason, Reason)
        for name in ("call_id", "resource_id"):
            value = getattr(self, name)
            if value is not None:
                _check_identifier(name, value)


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


@dataclass(frozen=True, slots=True, kw_only=True)
class Verification:
    """`checked` counts the events found good, in seq order, before the first break."""

    ok: bool
    checked: int
    first_bad_seq: int | None = None
    kind: ChainBreak | None = None


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
        its seq. Does not commit. If the event cannot be written, the caller's whole
        transaction is rolled back before the error is raised, so the action it records can
        never commit without it, however the caller handles the error. A session in
        autocommit mode is refused before anything is written.
        """
        if not isinstance(now, datetime):
            raise TypeError("now must be a datetime")
        if now.utcoffset() is None:
            raise ValueError("now must carry a time zone")
        try:
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
            await session.execute(
                insert(AuditEvent).values(
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
                    mac=self._mac(row),
                )
            )
        except Exception:
            await session.rollback()
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
        the first break. Reads only; the caller ends the transaction.
        """
        statement = (
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
            )
            .order_by(AuditEvent.seq)
            .execution_options(yield_per=VERIFY_BATCH_SIZE)
        )
        checked = 0
        previous_mac = ZERO_MAC
        result = await session.stream(statement)
        try:
            async for batch in result.partitions():
                for stored in batch:
                    row = ChainRow(
                        seq=stored.seq,
                        ts=stored.ts,
                        call_id=stored.call_id,
                        actor=stored.actor,
                        action=stored.action,
                        resource_type=stored.resource_type,
                        resource_id=stored.resource_id,
                        outcome=stored.outcome,
                        reason=stored.reason,
                        prev_mac=stored.prev_mac,
                    )
                    kind = self._break_at(row, stored.mac, checked, previous_mac)
                    if kind is not None:
                        return Verification(
                            ok=False, checked=checked, first_bad_seq=row.seq, kind=kind
                        )
                    previous_mac = stored.mac
                    checked += 1
        finally:
            await result.close()
        return Verification(ok=True, checked=checked)

    def _break_at(
        self, row: ChainRow, mac: str, checked: int, previous_mac: str
    ) -> ChainBreak | None:
        """The first rule `row` breaks, given that `checked` good events came before it.
        The MAC comes first: a row that was edited in any way is reported as edited.
        """
        if not self._is_authentic(row, mac):
            return ChainBreak.MAC_MISMATCH
        expected_seq = checked + 1
        if checked == 0 and (row.seq != 1 or row.prev_mac != ZERO_MAC):
            return ChainBreak.WRONG_START
        if row.seq < expected_seq:
            return ChainBreak.DUPLICATE
        if row.seq > expected_seq:
            return ChainBreak.GAP
        if row.prev_mac != previous_mac:
            return ChainBreak.PREV_MAC_MISMATCH
        return None
