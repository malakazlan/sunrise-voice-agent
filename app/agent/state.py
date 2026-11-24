"""What the agent knows about one call, and the only ways it may change.

A call starts with `new_call`. A state is immutable. Each transition is a function that checks
the move against the current state and returns a new state, or raises IllegalTransitionError.
The identity fields (`verified`, `patient_id`, `patient_first_name`) are set only by `verify`,
the intake -> serving move, and only while the office is open; no other transition touches
them. Every state, however it is made, is validated for consistency: verified goes with a
patient id and a first name, an open office and a phase a verified caller can be in. Errors
name fields and phases, never values.

The identity claim and the SSN step live in intake with the office open: `claim` stores what
the caller said and arms the step, the input guard's `SsnStep` collects the digits, and the
claim ends with `verify` or a failed attempt, or with any phase change. The SSN digits
(`pending_ssn4`, `verified_ssn4`) are excluded from every dump and from the text form, by their
declaration; validation errors never show input.

A new patient's call is the intake -> booking move: `is_new_patient` is set and the call can
never be verified. The move is refused while an identity claim is pending and for a locked
call, so the SSN step can never be left for a booking. Its details (`new_patient_details`, the
draft) are stored by `collect`, in booking only, and are hidden like the SSN digits. `offer` remembers the slots a caller was
offered (`offered_slots`): a verified caller in serving or booking, or a new patient with the
details. Every phase change ends the draft and the offered slots.

These rules protect against the model and the caller. Code that deliberately bypasses
validation (`model_construct`, `object.__setattr__`) can still forge a state.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Self

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


# The phases a verified caller can be in: serving, where verify puts them, and every phase
# reachable from there.
VERIFIED_PHASES = frozenset(
    {Phase.SERVING, Phase.BOOKING, Phase.EMERGENCY, Phase.HANDOFF, Phase.CLOSING, Phase.ENDED}
)
# The phases a new patient's call can be in: booking, where intake moves it, and every phase
# reachable from there.
NEW_PATIENT_PHASES = frozenset(
    {Phase.BOOKING, Phase.EMERGENCY, Phase.HANDOFF, Phase.CLOSING, Phase.ENDED}
)
# Where a caller may have been offered slots.
OFFER_PHASES = frozenset({Phase.SERVING, Phase.BOOKING})


class Caller(StrEnum):
    """Who is calling, as far as code knows."""

    # Verified: a patient of the clinic, identified by code.
    PATIENT = "patient"
    # A first-time caller, booking, before and after their details are collected.
    NEW_PATIENT = "new_patient"
    NEW_PATIENT_WITH_DETAILS = "new_patient_with_details"
    # Not verified: with an identity claim pending (the SSN step armed or awaiting submit), with
    # verification locked, or neither.
    CLAIMANT = "claimant"
    LOCKED = "locked"
    UNIDENTIFIED = "unidentified"


class IllegalTransitionError(ValueError):
    """A move the current state does not allow."""


_FROZEN = ConfigDict(frozen=True, strict=True, extra="forbid", hide_input_in_errors=True)

# At this many failed attempts the call is locked: no more claims, no verification.
MAX_VERIFY_ATTEMPTS = 3

# The digits the SSN step has collected, and the four a verified caller gave.
_PENDING_DIGITS = r"^[0-9]{1,4}$"
_VERIFIED_DIGITS = r"^[0-9]{4}$"


def _hidden_digits(pattern: str) -> Any:
    """A field for SSN digits: never in a dump, never in the text form."""
    return Field(default=None, exclude=True, repr=False, pattern=pattern)


class IdentityClaim(BaseModel):
    """Who the caller says they are, as verify_identity took it. Also the transition that
    stores it and arms the SSN step."""

    model_config = _FROZEN

    full_name: str = Field(repr=False)
    dob: date = Field(repr=False)


class NewPatientDetails(BaseModel):
    """A new patient's details as collect_new_patient took them, checked and normalized: the
    draft the booking saves. Also the transition that stores it. None of it is in the text
    form."""

    model_config = _FROZEN

    first_name: str = Field(repr=False)
    last_name: str = Field(repr=False)
    dob: date = Field(repr=False)
    phone: str = Field(repr=False)
    email: str = Field(repr=False)
    insurance_provider: str | None = Field(repr=False)
    member_id: str | None = Field(repr=False)
    self_pay: bool = Field(repr=False)
    reason_short: str = Field(repr=False)


class SlotsOffered(BaseModel):
    """The slots find_slots offered, by id: the transition that adds them to `offered_slots`."""

    model_config = _FROZEN

    slot_ids: tuple[int, ...]


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
    tool_refusals: int = Field(default=0, ge=0)
    pending_claim: IdentityClaim | None = None
    expecting_ssn4: bool = False
    # Armed turns so far that have not completed the four digits.
    ssn_turns: int = Field(default=0, ge=0)
    pending_ssn4: str | None = _hidden_digits(_PENDING_DIGITS)
    verified_ssn4: str | None = _hidden_digits(_VERIFIED_DIGITS)
    # Never in a dump, never in the text form.
    new_patient_details: NewPatientDetails | None = Field(default=None, exclude=True, repr=False)
    # Each slot id find_slots returned in this call, once, in the order first offered.
    offered_slots: tuple[int, ...] = ()

    @field_validator("call_id")
    @classmethod
    def _call_id_has_its_shape(cls, value: str) -> str:
        if not CALL_ID.fullmatch(value):
            raise ValueError("call_id does not have the declared shape of a call id")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        identified = (self.patient_id is not None, self.patient_first_name is not None)
        if identified != (self.verified, self.verified):
            raise ValueError("patient_id and patient_first_name are set exactly when verified")
        if self.verified:
            if not self.office_open:
                raise ValueError("a caller is verified only while the office is open")
            if self.phase not in VERIFIED_PHASES:
                raise ValueError(f"a verified caller is not in {self.phase.value}")
            if self.patient_id is None or not PATIENT_ID.fullmatch(self.patient_id):
                raise ValueError("patient_id does not have the declared shape of a patient id")
        if self.verified_ssn4 is not None and not self.verified:
            raise ValueError("verified_ssn4 is set only for a verified caller")
        self._consistent_step()
        self._consistent_booking()
        return self

    def _consistent_booking(self) -> None:
        if self.is_new_patient:
            if self.verified:
                raise ValueError("a new patient's call is never verified")
            if self.phase not in NEW_PATIENT_PHASES:
                raise ValueError(f"a new patient's call is not in {self.phase.value}")
        if self.new_patient_details is not None and not (
            self.is_new_patient and self.phase is Phase.BOOKING
        ):
            raise ValueError("new_patient_details exist only in a new patient's call, in booking")
        if self.offered_slots and caller_of(self) not in (
            Caller.PATIENT,
            Caller.NEW_PATIENT_WITH_DETAILS,
        ):
            raise ValueError("offered_slots are kept only for a patient or a new patient's details")
        if self.offered_slots and self.phase not in OFFER_PHASES:
            raise ValueError("offered_slots are kept only in serving and booking")
        if len(set(self.offered_slots)) != len(self.offered_slots):
            raise ValueError("offered_slots holds each slot once")

    def _consistent_step(self) -> None:
        claimed = self.pending_claim is not None
        stepped = (claimed, self.expecting_ssn4, self.ssn_turns != 0, self.pending_ssn4 is not None)
        if any(stepped) and not (self.phase is Phase.INTAKE and self.office_open):
            raise ValueError("the claim and the SSN step exist only in intake, office open")
        if (self.expecting_ssn4 or self.pending_ssn4 is not None) and not claimed:
            raise ValueError("expecting_ssn4 and pending_ssn4 need a pending_claim")
        if self.ssn_turns and not self.expecting_ssn4:
            raise ValueError("ssn_turns are counted only while expecting_ssn4")
        if self.verify_locked != (self.verify_attempts >= MAX_VERIFY_ATTEMPTS):
            raise ValueError("verify_locked is set exactly when verify_attempts reaches the limit")
        if self.verify_locked and claimed:
            raise ValueError("a locked call holds no pending_claim")


def caller_of(state: CallState) -> Caller | None:
    """Who is calling. None for a state that is both a patient's and a new patient's, or
    verified without a patient id: no transition makes one, and no tool is offered to it."""
    if state.verified:
        consistent = state.patient_id is not None and not state.is_new_patient
        return Caller.PATIENT if consistent else None
    if state.is_new_patient:
        if state.new_patient_details is None:
            return Caller.NEW_PATIENT
        return Caller.NEW_PATIENT_WITH_DETAILS
    if state.verify_locked:
        return Caller.LOCKED
    if state.pending_claim is not None:
        return Caller.CLAIMANT
    return Caller.UNIDENTIFIED


def new_call(*, call_id: str, started_at: datetime, office_open: bool) -> CallState:
    """A call's first state: greeting, unverified, nothing counted."""
    return CallState(call_id=call_id, started_at=started_at, office_open=office_open)


def _next(state: CallState, **changes: Any) -> CallState:
    """The state with `changes`, validated like any other. Every field is carried, the hidden
    ones included."""
    return CallState.model_validate({**dict(state), **changes})


# What ends the identity claim and the SSN step.
_NO_STEP: dict[str, Any] = {
    "pending_claim": None,
    "expecting_ssn4": False,
    "ssn_turns": 0,
    "pending_ssn4": None,
}
# What a phase change ends besides: the new patient's draft and the slots offered.
_NO_BOOKING: dict[str, Any] = {"new_patient_details": None, "offered_slots": ()}


# The moves `move` makes, from each phase. intake -> serving is not here: it is `verify`. The
# caller can hang up at any time, so every phase but ended moves to ended.
_MOVES: dict[Phase, frozenset[Phase]] = {
    Phase.GREETING: frozenset({Phase.INTAKE, Phase.EMERGENCY, Phase.ENDED}),
    Phase.INTAKE: frozenset(
        {Phase.BOOKING, Phase.CLOSING, Phase.HANDOFF, Phase.EMERGENCY, Phase.ENDED}
    ),
    Phase.SERVING: frozenset(
        {Phase.BOOKING, Phase.CLOSING, Phase.HANDOFF, Phase.EMERGENCY, Phase.ENDED}
    ),
    Phase.BOOKING: frozenset(
        {Phase.SERVING, Phase.CLOSING, Phase.HANDOFF, Phase.EMERGENCY, Phase.ENDED}
    ),
    Phase.EMERGENCY: frozenset({Phase.EMERGENCY, Phase.ENDED}),
    Phase.HANDOFF: frozenset({Phase.EMERGENCY, Phase.ENDED}),
    Phase.CLOSING: frozenset({Phase.EMERGENCY, Phase.ENDED}),
    Phase.ENDED: frozenset(),
}


def _refuse(state: CallState, to: Phase, why: str = "not a legal move") -> IllegalTransitionError:
    return IllegalTransitionError(f"{state.phase.value} -> {to.value} is {why}")


def move(state: CallState, to: Phase) -> CallState:
    """Every legal move except intake -> serving. Hand-off needs the office open; intake ->
    booking is a new patient's booking, refused while an identity claim is pending (the SSN
    step is armed, or its digits await the submit: leaving would let them reach the model) and
    for a call whose verification is locked; booking -> serving needs a verified caller.
    Every move ends the identity claim and the SSN step, the new patient's draft and the slots
    offered."""
    if to not in _MOVES[state.phase]:
        raise _refuse(state, to)
    if to is Phase.HANDOFF and not state.office_open:
        raise _refuse(state, to, "refused while the office is closed")
    if to is Phase.SERVING and not state.verified:
        raise _refuse(state, to, "refused for a caller who is not verified")
    if state.phase is Phase.INTAKE and to is Phase.BOOKING:
        if state.pending_claim is not None:
            raise _refuse(state, to, "refused while an identity claim is pending")
        if state.verify_locked:
            raise _refuse(state, to, "refused for a locked call")
        return _next(state, phase=to, is_new_patient=True, **_NO_STEP, **_NO_BOOKING)
    return _next(state, phase=to, **_NO_STEP, **_NO_BOOKING)


def verify(state: CallState, *, patient_id: str, first_name: str) -> CallState:
    """intake -> serving for the patient code identified: the only transition that sets
    `verified`, `patient_id` and `patient_first_name`. The digits the step collected are kept
    as `verified_ssn4`; the claim and the step end."""
    if state.phase is not Phase.INTAKE:
        raise _refuse(state, Phase.SERVING)
    if not state.office_open:
        raise _refuse(state, Phase.SERVING, "refused while the office is closed")
    if state.verify_locked:
        raise _refuse(state, Phase.SERVING, "refused for a locked call")
    if not isinstance(patient_id, str) or not PATIENT_ID.fullmatch(patient_id):
        raise ValueError("patient_id does not have the declared shape of a patient id")
    if not isinstance(first_name, str) or not first_name.strip():
        raise ValueError("first_name must not be empty")
    return _next(
        state,
        phase=Phase.SERVING,
        verified=True,
        patient_id=patient_id,
        patient_first_name=first_name,
        verified_ssn4=state.pending_ssn4,
        **_NO_STEP,
    )


def _refuse_step(state: CallState, what: str, why: str) -> IllegalTransitionError:
    return IllegalTransitionError(f"{what} in {state.phase.value} is {why}")


def claim(state: CallState, identity: IdentityClaim) -> CallState:
    """Store who the caller says they are and arm the SSN step: intake only, office open, a
    call that is not locked. A second claim replaces the first and clears the digits."""
    if state.phase is not Phase.INTAKE:
        raise _refuse_step(state, "a claim", "not a legal move")
    if not state.office_open:
        raise _refuse_step(state, "a claim", "refused while the office is closed")
    if state.verify_locked:
        raise _refuse_step(state, "a claim", "refused for a locked call")
    return _next(state, pending_claim=identity, expecting_ssn4=True, ssn_turns=0, pending_ssn4=None)


def locks_the_call(state: CallState) -> bool:
    """Whether one more failed attempt locks the call."""
    return state.verify_attempts + 1 >= MAX_VERIFY_ATTEMPTS


def fail_attempt(state: CallState) -> CallState:
    """One failed attempt, in intake: counted, the call locked at MAX_VERIFY_ATTEMPTS, and the
    claim and the step ended."""
    if state.phase is not Phase.INTAKE:
        raise _refuse_step(state, "a failed attempt", "not a legal move")
    return _next(
        state,
        verify_attempts=state.verify_attempts + 1,
        verify_locked=locks_the_call(state),
        **_NO_STEP,
    )


def drop_claim(state: CallState) -> CallState:
    """End the claim and the step without counting anything: for a claim found locked, and a
    submit that did not run."""
    return _next(state, **_NO_STEP)


def collect(state: CallState, details: NewPatientDetails) -> CallState:
    """Store a new patient's details: in a new patient's call, in booking, with the office
    open. Details collected again replace the first, and the slots offered so far go."""
    if state.phase is not Phase.BOOKING or not state.is_new_patient:
        raise _refuse_step(state, "new patient details", "not a legal move")
    if not state.office_open:
        raise _refuse_step(state, "new patient details", "refused while the office is closed")
    return _next(state, new_patient_details=details, offered_slots=())


def offer(state: CallState, offered: SlotsOffered) -> CallState:
    """Remember the slots offered, after those offered before: for a verified caller in serving
    or booking, or a new patient with the details collected, with the office open."""
    if state.phase not in OFFER_PHASES or caller_of(state) not in (
        Caller.PATIENT,
        Caller.NEW_PATIENT_WITH_DETAILS,
    ):
        raise _refuse_step(state, "an offer of slots", "not a legal move")
    if not state.office_open:
        raise _refuse_step(state, "an offer of slots", "refused while the office is closed")
    known = state.offered_slots
    added = tuple(dict.fromkeys(slot for slot in offered.slot_ids if slot not in known))
    return _next(state, offered_slots=known + added)


# The counters change nothing the consistency rules look at, so counting never fails: dispatch
# counts on whatever state it is given and always returns a result.
def count_tool_error(state: CallState) -> CallState:
    return state.model_copy(update={"tool_errors": state.tool_errors + 1})


def count_refusal(state: CallState) -> CallState:
    return state.model_copy(update={"tool_refusals": state.tool_refusals + 1})


class Move(BaseModel):
    """A request for `move`."""

    model_config = _FROZEN

    to: Phase


class Verify(BaseModel):
    """A handler's request for `verify`. Only the tool declared to verify may make it."""

    model_config = _FROZEN

    patient_id: str
    first_name: str = Field(repr=False)


class FailedAttempt(BaseModel):
    """A handler's request for `fail_attempt`."""

    model_config = _FROZEN


class EndClaim(BaseModel):
    """A handler's request for `drop_claim`."""

    model_config = _FROZEN


class SsnStep(BaseModel):
    """The input guard's change to an armed SSN step: the step's fields as they are after the
    turn. Applied by code, never returned by a tool."""

    model_config = _FROZEN

    expecting_ssn4: bool
    ssn_turns: int = Field(ge=0)
    pending_ssn4: str | None = _hidden_digits(_PENDING_DIGITS)


def _step(state: CallState, change: SsnStep) -> CallState:
    if not state.expecting_ssn4:
        raise _refuse_step(state, "an SSN step change", "refused while the step is not armed")
    return _next(
        state,
        expecting_ssn4=change.expecting_ssn4,
        ssn_turns=change.ssn_turns,
        pending_ssn4=change.pending_ssn4,
    )


Transition = (
    Move
    | Verify
    | IdentityClaim
    | FailedAttempt
    | EndClaim
    | SsnStep
    | NewPatientDetails
    | SlotsOffered
)


def apply(state: CallState, transition: Transition) -> CallState:
    """A transition given as a value, through the same function as any other caller."""
    if isinstance(transition, Move):
        return move(state, transition.to)
    if isinstance(transition, Verify):
        return verify(state, patient_id=transition.patient_id, first_name=transition.first_name)
    if isinstance(transition, IdentityClaim):
        return claim(state, transition)
    if isinstance(transition, NewPatientDetails):
        return collect(state, transition)
    if isinstance(transition, SlotsOffered):
        return offer(state, transition)
    if isinstance(transition, FailedAttempt):
        return fail_attempt(state)
    if isinstance(transition, EndClaim):
        return drop_claim(state)
    if isinstance(transition, SsnStep):
        return _step(state, transition)
    raise TypeError("not a transition")
