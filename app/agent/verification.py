"""Identity verification: the claim, and the check of the claim with the SSN digits.

`verify_identity` (the model calls it with what the caller said) stores the claimed full name
and date of birth and arms the SSN step, unless the call is locked or the claimed identity has
failed MAX_CLAIM_FAILURES times within CLAIM_WINDOW, across calls; then it ends any claim
the call held. It never reads a patient record, so it never says whether a patient exists.

`submit_identity` (only code calls it, when the input guard says submit) reads the claim and
the digits from the state. It selects the records with exactly that date of birth and those last
four digits, and `pick_patient` must find exactly one by name. A match verifies the caller;
anything else is one failed attempt, the same in every way whatever did not match, and the
third locks the call. Each submit is recorded in `verification_attempts` under the claim's keyed
hash (app/agent/claims.py), in dispatch's transaction with the state change and the audit event.
The count across calls is checked again there, under a transaction-level advisory lock on the
claim's key taken before anything else (before the audit log's lock): calls armed at the same
time submit one after another, and once the identity is locked a submit counts nothing.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)
from sqlalchemy import Integer, cast, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.scripts import ScriptId
from app.agent.state import (
    CallState,
    EndClaim,
    FailedAttempt,
    IdentityClaim,
    Verify,
    locks_the_call,
)
from app.agent.tool_registry import HandlerContext, NoArguments, ToolResult
from app.audit.log import Action
from app.db.models import Patient, VerificationAttempt
from app.domain.matching import MAX_NAME_LENGTH, name_key, pick_patient

# Failed attempts for one claimed identity within the window, across calls, that lock it.
MAX_CLAIM_FAILURES = 5
CLAIM_WINDOW = timedelta(hours=1)

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The advisory locks on claims are two-integer keys under this first key, "idnt": a key space
# of their own, apart from the audit log's single-key lock.
CLAIM_LOCKS = int.from_bytes(b"idnt", "big")


def _iso_text(value: object) -> date:
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise ValueError("dob must be an ISO date, YYYY-MM-DD")
    return date.fromisoformat(value)


def _in_range(value: date, info: ValidationInfo) -> date:
    today = (info.context or {}).get("today")
    if not isinstance(today, date):
        raise ValueError("dob is checked against the clinic's date, which is missing")
    if value > today:
        raise ValueError("dob is in the future")
    if value < EARLIEST_DOB:
        raise ValueError("dob is before 1900")
    return value


# The earliest date of birth taken.
EARLIEST_DOB = date(1900, 1, 1)

# A date of birth as the caller gives it: strictly an ISO date (YYYY-MM-DD), from 1900-01-01 to
# the clinic's date, which dispatch passes in the validation context.
DateOfBirth = Annotated[date, BeforeValidator(_iso_text), AfterValidator(_in_range)]


class VerifyIdentityArguments(BaseModel):
    """The caller's full name, and their date of birth as an ISO date (YYYY-MM-DD)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    full_name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    dob: DateOfBirth

    @field_validator("full_name")
    @classmethod
    def _is_a_name(cls, value: str) -> str:
        if name_key(value) is None:
            raise ValueError("full_name is not a name")
        return value


# Locked: nothing armed, and any claim the call held ends with it.
_LOCKED = ToolResult(
    transition=EndClaim(), script=ScriptId.VERIFY_LOCKED, audit_action=Action.IDENTITY_LOCKED
)


async def _failures(session: AsyncSession, claim_key: str, context: HandlerContext) -> int:
    """The claim's failed attempts within the window, across calls."""
    return (
        await session.execute(
            select(func.count())
            .select_from(VerificationAttempt)
            .where(
                VerificationAttempt.claim_key == claim_key,
                VerificationAttempt.success.is_(False),
                VerificationAttempt.created_at >= context.now - CLAIM_WINDOW,
            )
        )
    ).scalar_one()


async def _lock_claim(session: AsyncSession, claim_key: str) -> None:
    """Wait for the claim's advisory lock, held to the end of the transaction."""
    second = int.from_bytes(bytes.fromhex(claim_key[:8]), "big", signed=True)
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                cast(literal(CLAIM_LOCKS), Integer), cast(literal(second), Integer)
            )
        )
    )


async def verify_identity(
    session: AsyncSession,
    state: CallState,
    arguments: VerifyIdentityArguments,
    context: HandlerContext,
) -> ToolResult:
    if state.verify_locked:
        return _LOCKED
    claim_key = context.claim_keys.key_for(arguments.full_name, arguments.dob)
    if await _failures(session, claim_key, context) >= MAX_CLAIM_FAILURES:
        return _LOCKED
    return ToolResult(
        transition=IdentityClaim(full_name=arguments.full_name, dob=arguments.dob),
        script=ScriptId.SSN_ASK,
    )


@dataclass(frozen=True, slots=True)
class _Candidate:
    id: str
    first_name: str
    last_name: str


async def submit_identity(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    claim = state.pending_claim
    if claim is None:
        raise LookupError("there is no claim to submit")
    digits = state.pending_ssn4 or ""
    claim_key = context.claim_keys.key_for(claim.full_name, claim.dob)
    await _lock_claim(session, claim_key)
    if await _failures(session, claim_key, context) >= MAX_CLAIM_FAILURES:
        return _LOCKED
    rows = (
        await session.execute(
            select(Patient.id, Patient.first_name, Patient.last_name).where(
                Patient.dob == claim.dob, Patient.ssn_last4 == digits
            )
        )
    ).all()
    patient = pick_patient(claim.full_name, [_Candidate(*row) for row in rows])
    await session.execute(
        insert(VerificationAttempt).values(
            claim_key=claim_key,
            call_id=state.call_id,
            success=patient is not None,
            created_at=context.now,
        )
    )
    if patient is not None:
        return ToolResult(
            transition=Verify(patient_id=patient.id, first_name=patient.first_name),
            script=ScriptId.VERIFIED,
            script_values={"first_name": patient.first_name},
            audit_action=Action.IDENTITY_VERIFIED,
        )
    if locks_the_call(state):
        return ToolResult(
            transition=FailedAttempt(),
            script=ScriptId.VERIFY_LOCKED,
            audit_action=Action.IDENTITY_LOCKED,
        )
    return ToolResult(
        transition=FailedAttempt(),
        script=ScriptId.VERIFY_FAIL,
        audit_action=Action.IDENTITY_REFUSED,
    )
