"""The admin's writes: a row of the requests' queue marked handled, a callback's details shown,
a config key changed. The admin API (app/api/admin.py) runs each in the transaction that also
appends its audit event; each takes that transaction's session and never ends it.

A mark and a change are inserted, never updated or deleted, and the runtime role may do neither
(app/db/grants.py). A row is marked once: a second mark finds the first. A key is changed only
to another of its values; changes are serialised, so two at once cannot both find the old one.

`callback_details` is the one read of the admin API that returns a caller's name and number
unmasked. It is kept out of app/api/reads.py, every value of which is masked, and one route
calls it.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, Text, and_, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CONFIG_VALUES, QUEUE_SOURCES, ConfigChange, HandledMark, Message, Request
from app.runtime_config import Setting, read_config

# The transaction-scoped advisory lock that serialises config changes: the bytes of "config".
CONFIG_LOCK = int.from_bytes(b"config", "big")


@dataclass(frozen=True, slots=True, kw_only=True)
class Mark:
    """A row's mark, and whether this request made it."""

    by: str
    at: datetime
    new: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Change:
    """A key's setting after a change was asked for, and whether it changed."""

    setting: Setting
    changed: bool


async def mark_handled(
    session: AsyncSession, source: str, row_id: int, by: str, now: datetime
) -> Mark | None:
    """The row marked handled by `by` at `now`, unless it was already: then its mark. None for
    a row that is not there."""
    if source not in QUEUE_SOURCES:
        return None
    # The queue's rows are requests: the mark is made only for a request that is there.
    there = select(
        literal(source, Text), Request.id, literal(by, Text), literal(now, DateTime(timezone=True))
    ).where(Request.id == row_id)
    added = await session.execute(
        insert(HandledMark)
        .from_select(["source", "row_id", "marked_by", "marked_at"], there)
        .on_conflict_do_nothing(index_elements=["source", "row_id"])
        .returning(HandledMark.marked_by, HandledMark.marked_at)
    )
    made = added.first()
    if made is not None:
        return Mark(by=made.marked_by, at=made.marked_at, new=True)
    found = (
        await session.execute(
            select(HandledMark.marked_by, HandledMark.marked_at).where(
                HandledMark.source == source, HandledMark.row_id == row_id
            )
        )
    ).first()
    return None if found is None else Mark(by=found.marked_by, at=found.marked_at, new=False)


async def callback_details(
    session: AsyncSession, source: str, row_id: int
) -> list[dict[str, str | None]] | None:
    """The caller's name and full number of each message the request was left with that has a
    number to call back, in the order they were left; None when it has none, or is not there."""
    if source not in QUEUE_SOURCES:
        return None
    left = (
        select(Message.name, Message.callback_number)
        .join(
            Request,
            and_(
                Request.call_id == Message.call_id,
                Request.type == Message.kind,
                Request.summary == Message.body,
            ),
        )
        .where(Request.id == row_id, Message.callback_number.is_not(None))
        .order_by(Message.id)
    )
    found = [
        {"name": row.name, "callback_number": row.callback_number}
        for row in await session.execute(left)
    ]
    return found or None


async def change_config(
    session: AsyncSession, key: str, value: str, by: str, now: datetime
) -> Change:
    """The key set to the value by `by` at `now`, unless it has that value already. A value
    that is not one of the key's is refused."""
    if value not in CONFIG_VALUES.get(key, ()):
        raise ValueError("the value is not one of the key's values")
    await session.execute(select(func.pg_advisory_xact_lock(CONFIG_LOCK)))
    current = (await read_config(session))[key]
    if current.value == value:
        return Change(setting=current, changed=False)
    added = await session.execute(
        insert(ConfigChange)
        .values(key=key, value=value, changed_by=by, changed_at=now)
        .returning(ConfigChange.changed_by, ConfigChange.changed_at)
    )
    made = added.one()
    return Change(
        setting=Setting(value=value, changed_by=made.changed_by, changed_at=made.changed_at),
        changed=True,
    )
