"""What the agent knows about one call, and the only ways it may change.

A state is immutable. Each transition is a function that checks the move against the current
state and returns a new state, or raises IllegalTransitionError. The identity fields (`verified`,
`patient_id`, `patient_first_name`) are set only by `verify`, the intake -> serving move, and
only while the office is open; a call starts without them and no other transition touches
them. Errors name fields and phases, never values.
"""

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.audit.log import CALL_ID, PATIENT_ID


class Phase(StrEnum):
    GREETING = "greeting"
    INTAKE = "intake"
    SERVING = "serving"
    BOOKING = "booking"
    EMERGENCY = "emergency"
    HANDOFF = "handoff"
    CLOSING = "closing"
    ENDED = "ended"


class IllegalTransitionError(ValueError):
    """A move the current state does not allow."""


_FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid", hide_input_in_errors=True)


class CallState(BaseModel):
    """`call_id` comes from the call registry (app/voice/calls.py), never from the caller or
    the model. `office_open` is decided by code when the call starts."""

    model_config = _FROZEN

    call_id: str
    started_at: AwareDatetime
    office_open: bool
    phase: Phase = Phase.GREETING
    verified: bool = False
    verify_attempts: int = Field(default=0, ge=0)
    verify_locked: bool = False
    patient_id: str | None = None
    patient_first_name: str | None = Field(default=None, repr=False)
    is_new_patient: bool = False
    tool_errors: int = Field(default=0, ge=0)

    @field_validator("call_id")
    @classmethod
    def _call_id_has_its_shape(cls, value: str) -> str:
        if not CALL_ID.fullmatch(value):
            raise ValueError("call_id does not have the declared shape of a call id")
        return value

    @model_validator(mode="after")
    def _starts_unverified(self) -> Self:
        if self.verified or self.patient_id is not None or self.patient_first_name is not None:
            raise ValueError("a call starts unverified; only verify sets the patient")
        return self


# The moves `move` makes, from each phase. intake -> serving is not here: it is `verify`.
_MOVES: dict[Phase, frozenset[Phase]] = {
    Phase.GREETING: frozenset({Phase.INTAKE, Phase.EMERGENCY}),
    Phase.INTAKE: frozenset({Phase.BOOKING, Phase.CLOSING, Phase.HANDOFF, Phase.EMERGENCY}),
    Phase.SERVING: frozenset({Phase.BOOKING, Phase.CLOSING, Phase.HANDOFF, Phase.EMERGENCY}),
    Phase.BOOKING: frozenset({Phase.SERVING, Phase.CLOSING, Phase.HANDOFF, Phase.EMERGENCY}),
    Phase.EMERGENCY: frozenset({Phase.EMERGENCY, Phase.ENDED}),
    Phase.HANDOFF: frozenset({Phase.EMERGENCY, Phase.ENDED}),
    Phase.CLOSING: frozenset({Phase.EMERGENCY, Phase.ENDED}),
    Phase.ENDED: frozenset(),
}


def _refuse(state: CallState, to: Phase, why: str = "not a legal move") -> IllegalTransitionError:
    return IllegalTransitionError(f"{state.phase.value} -> {to.value} is {why}")


def move(state: CallState, to: Phase) -> CallState:
    """Every legal move except intake -> serving. Hand-off needs the office open; intake ->
    booking is a new patient's booking, and booking -> serving needs a verified caller."""
    if to not in _MOVES[state.phase]:
        raise _refuse(state, to)
    if to is Phase.HANDOFF and not state.office_open:
        raise _refuse(state, to, "refused while the office is closed")
    if to is Phase.SERVING and not state.verified:
        raise _refuse(state, to, "refused for a caller who is not verified")
    if state.phase is Phase.INTAKE and to is Phase.BOOKING:
        return state.model_copy(update={"phase": to, "is_new_patient": True})
    return state.model_copy(update={"phase": to})


def verify(state: CallState, *, patient_id: str, first_name: str) -> CallState:
    """intake -> serving for the patient code identified: the only transition that sets
    `verified`, `patient_id` and `patient_first_name`."""
    if state.phase is not Phase.INTAKE:
        raise _refuse(state, Phase.SERVING)
    if not state.office_open:
        raise _refuse(state, Phase.SERVING, "refused while the office is closed")
    if not isinstance(patient_id, str) or not PATIENT_ID.fullmatch(patient_id):
        raise ValueError("patient_id does not have the declared shape of a patient id")
    if not isinstance(first_name, str) or not first_name.strip():
        raise ValueError("first_name must not be empty")
    return state.model_copy(
        update={
            "phase": Phase.SERVING,
            "verified": True,
            "patient_id": patient_id,
            "patient_first_name": first_name,
        }
    )


def count_tool_error(state: CallState) -> CallState:
    return state.model_copy(update={"tool_errors": state.tool_errors + 1})


class Move(BaseModel):
    """A handler's request for `move`."""

    model_config = _FROZEN

    to: Phase


class Verify(BaseModel):
    """A handler's request for `verify`."""

    model_config = _FROZEN

    patient_id: str
    first_name: str = Field(repr=False)


def apply(state: CallState, transition: Move | Verify) -> CallState:
    """The transition a handler asked for, through the same function as any other caller."""
    if isinstance(transition, Move):
        return move(state, transition.to)
    if isinstance(transition, Verify):
        return verify(state, patient_id=transition.patient_id, first_name=transition.first_name)
    raise TypeError("a transition is a Move or a Verify")
