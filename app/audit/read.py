"""Reading the audit log for the admin API: one page of events, newest first.

Every filter is checked here against the audit registries before a statement is built, and
every value reaches the database as a bound parameter.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.log import CALL_ID, Action
from app.db.models import AuditEvent

MAX_PAGE = 100
# The largest value a bigint seq can hold.
MAX_SEQ = 2**63 - 1


async def recent_events(
    session: AsyncSession,
    *,
    limit: int,
    before: int | None,
    call_id: str | None,
    action: Action | None,
) -> list[dict[str, Any]]:
    """At most `limit` events with a seq below `before`, newest first, optionally only those
    of one call or one action. Every column except the MACs."""
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit must be 1 to {MAX_PAGE}")
    if before is not None and (type(before) is not int or not 1 <= before <= MAX_SEQ):
        raise ValueError("before must be a positive seq")
    if call_id is not None and not (isinstance(call_id, str) and CALL_ID.fullmatch(call_id)):
        raise ValueError("call_id does not have the shape of a call id")
    if action is not None and not isinstance(action, Action):
        raise TypeError("action must be a member of Action")

    statement = select(
        AuditEvent.seq,
        AuditEvent.ts,
        AuditEvent.call_id,
        AuditEvent.actor,
        AuditEvent.action,
        AuditEvent.resource_type,
        AuditEvent.resource_id,
        AuditEvent.outcome,
        AuditEvent.reason,
    )
    if before is not None:
        statement = statement.where(AuditEvent.seq < before)
    if call_id is not None:
        statement = statement.where(AuditEvent.call_id == call_id)
    if action is not None:
        statement = statement.where(AuditEvent.action == action.value)
    rows = await session.execute(statement.order_by(AuditEvent.seq.desc()).limit(limit))
    return [dict(row._mapping) for row in rows]
