"""The admin's data functions: what the admin API (app/api/admin.py) and the console
(app/console/) read, with the same bounds and the same masking. Each reads in the session it is
given, from statements with bound parameters only; the caller runs it through AdminAccess.read,
which writes the audit event in the same transaction.

Every text value passes `mask` on its way out, and a callback number shows only its last four
digits. What only the console shows (`call_detail`, `request_queue`) also hides the names its
rows know, wherever they appear: each message's caller name and each patient's first and last
name. The caller's name itself is never returned by them.
"""

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.audit.log import CALL_ID, Action
from app.audit.read import MAX_PAGE, MAX_SEQ, recent_events
from app.db import models
from app.guard.mask import hide_names, mask

DEFAULT_PAGE = 50
# The largest value of a Postgres integer, the type of request, message and call ids.
MAX_ROW_ID = 2**31 - 1
DIGIT = re.compile(r"\d")

REQUEST_COLUMNS = (
    models.Request.id,
    models.Request.created_at,
    models.Request.call_id,
    models.Request.type,
    models.Request.summary,
    models.Request.patient_id,
)
MESSAGE_COLUMNS = (
    models.Message.id,
    models.Message.created_at,
    models.Message.call_id,
    models.Message.kind,
    models.Message.name,
    models.Message.callback_number,
    models.Message.body,
    models.Message.patient_id,
)
CALL_COLUMNS = (
    models.CallSession.id,
    models.CallSession.call_id,
    models.CallSession.started_at,
    models.CallSession.ended_at,
    models.CallSession.office_open,
    models.CallSession.verified,
    models.CallSession.patient_id,
    models.CallSession.end_reason,
    models.CallSession.turn_count,
)


class Page(BaseModel):
    """A page of requests, messages or calls: ids are Postgres integers."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    before: int | None = Field(default=None, ge=1, le=MAX_ROW_ID)


class AuditPage(Page):
    """A page of audit events: seqs are bigints."""

    before: int | None = Field(default=None, ge=1, le=MAX_SEQ)
    call_id: str | None = None
    action: Action | None = None

    @field_validator("call_id")
    @classmethod
    def _call_id_shape(cls, value: str | None) -> str | None:
        if value is not None and not CALL_ID.fullmatch(value):
            raise ValueError("call_id does not have the shape of a call id")
        return value


def masked(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row as the admin sees it: text masked; instants, numbers, flags and nulls as they are.
    Any other type stops the read."""
    shown: dict[str, Any] = {}
    for name, value in row.items():
        if isinstance(value, str):
            shown[name] = mask(value)
        elif value is None or isinstance(value, int | datetime):
            shown[name] = value
        else:
            raise TypeError(f"{name} has a type the admin is not shown")
    return shown


def shown_callback(value: str | None) -> str | None:
    """A callback number as `***-***-` and its last four digits, however it was written;
    with fewer than four digits, nothing of it."""
    if value is None:
        return None
    digits = DIGIT.findall(value)
    return "***-***-" + ("".join(digits[-4:]) if len(digits) >= 4 else "****")


async def newest(
    session: AsyncSession,
    columns: Sequence[InstrumentedAttribute[Any]],
    row_id: InstrumentedAttribute[int],
    page: Page,
    *conditions: ColumnElement[bool],
) -> list[dict[str, Any]]:
    """At most `page.limit` rows with an id below `page.before`, newest first, of those that
    meet every condition."""
    statement = select(*columns).where(*conditions)
    if page.before is not None:
        statement = statement.where(row_id < page.before)
    rows = await session.execute(statement.order_by(row_id.desc()).limit(page.limit))
    return [dict(row._mapping) for row in rows]


async def call(session: AsyncSession, call_id: str) -> dict[str, Any] | None:
    """The stored call with its transcript, or None."""
    statement = select(
        *CALL_COLUMNS,
        models.CallSession.transcript,
        models.CallSession.transcript_truncated,
        models.CallSession.names_masked,
    ).where(models.CallSession.call_id == call_id)
    row = (await session.execute(statement)).first()
    return None if row is None else dict(row._mapping)


async def _known_names(
    session: AsyncSession, call_ids: set[str], patient_ids: set[str]
) -> list[str]:
    """What a page about these calls and patients must hide: every caller name the calls'
    messages hold, and the first and last names of the patients, of those the calls' messages
    and requests name, and of the patient each call verified."""
    names: list[str] = []
    patients = set(patient_ids)
    if call_ids:
        calls = sorted(call_ids)
        left = await session.execute(
            select(models.Message.name, models.Message.patient_id).where(
                models.Message.call_id.in_(calls)
            )
        )
        for name, patient_id in left:
            if name is not None:
                names += [name]
            if patient_id is not None:
                patients |= {patient_id}
        named = select(models.Request.patient_id).where(models.Request.call_id.in_(calls))
        verified = select(models.CallSession.patient_id).where(
            models.CallSession.call_id.in_(calls)
        )
        for statement in (named, verified):
            patients |= {found for found in await session.scalars(statement) if found is not None}
    if patients:
        rows = await session.execute(
            select(models.Patient.first_name, models.Patient.last_name).where(
                models.Patient.id.in_(sorted(patients))
            )
        )
        for first_name, last_name in rows:
            names += [first_name, last_name]
    return names


async def ended_calls(
    session: AsyncSession, page: Page, end_reason: str | None
) -> list[dict[str, Any]]:
    """A page of the calls, newest first, of those that ended for `end_reason` if one is given.
    No transcript."""
    if end_reason is not None and end_reason not in models.CALL_END_REASONS:
        raise ValueError("end_reason is not one a call ends for")
    conditions = [] if end_reason is None else [models.CallSession.end_reason == end_reason]
    rows = await newest(session, CALL_COLUMNS, models.CallSession.id, page, *conditions)
    return [masked(row) for row in rows]


async def call_detail(session: AsyncSession, call_id: str) -> dict[str, Any] | None:
    """The call, its transcript and its audit events (the latest MAX_PAGE of them, oldest
    first), or None. The transcript is None when its names were not masked when it was stored:
    it is not shown until they are."""
    if not CALL_ID.fullmatch(call_id):
        raise ValueError("call_id does not have the shape of a call id")
    row = await call(session, call_id)
    if row is None:
        return None
    events = await recent_events(session, limit=MAX_PAGE, before=None, call_id=call_id, action=None)
    transcript = row.pop("transcript")
    shown = masked(row)
    if row["names_masked"]:
        patients = set() if row["patient_id"] is None else {row["patient_id"]}
        names = await _known_names(session, {call_id}, patients)
        shown["transcript"] = hide_names(mask(transcript), names)
    else:
        shown["transcript"] = None
    shown["events"] = [masked(event) for event in reversed(events)]
    return shown


async def request_queue(
    session: AsyncSession, page: Page, request_type: str | None
) -> list[dict[str, Any]]:
    """A page of the requests, newest first, of `request_type` if one is given. A callback or
    a doctor's message carries the masked numbers of the messages it was left with
    (`callbacks`) and whether one was left after hours; never the caller's name."""
    if request_type is not None and request_type not in models.REQUEST_TYPES:
        raise ValueError("request_type is not a type of request")
    conditions = [] if request_type is None else [models.Request.type == request_type]
    rows = await newest(session, REQUEST_COLUMNS, models.Request.id, page, *conditions)
    call_ids = {row["call_id"] for row in rows}
    messages = []
    if call_ids:
        statement = select(
            models.Message.call_id,
            models.Message.kind,
            models.Message.body,
            models.Message.callback_number,
            models.Message.after_hours,
        ).where(models.Message.call_id.in_(sorted(call_ids)))
        messages = list(await session.execute(statement))
    patients = {row["patient_id"] for row in rows if row["patient_id"] is not None}
    names = await _known_names(session, call_ids, patients)
    return [_queued(row, messages, names) for row in rows]


def _queued(row: Mapping[str, Any], messages: Sequence[Any], names: list[str]) -> dict[str, Any]:
    """A request as the queue shows it, with what the messages it was left with add."""
    left = [
        message
        for message in messages
        if (message.call_id, message.kind, message.body)
        == (row["call_id"], row["type"], row["summary"])
    ]
    shown = masked(row)
    shown["summary"] = hide_names(shown["summary"], names)
    shown["callbacks"] = sorted(
        {
            number
            for message in left
            if (number := shown_callback(message.callback_number)) is not None
        }
    )
    shown["after_hours"] = any(message.after_hours for message in left)
    return shown
