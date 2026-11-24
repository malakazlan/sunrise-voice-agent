from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from app.agent.state import (
    CallState,
    IllegalTransitionError,
    Move,
    Phase,
    Verify,
    apply,
    count_refusal,
    count_tool_error,
    move,
    new_call,
    verify,
)
from app.voice.calls import CallRegistry

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)

GREETING = Phase.GREETING
INTAKE = Phase.INTAKE
SERVING = Phase.SERVING
BOOKING = Phase.BOOKING
EMERGENCY = Phase.EMERGENCY
HANDOFF = Phase.HANDOFF
CLOSING = Phase.CLOSING
ENDED = Phase.ENDED


def new_call_id() -> str:
    """A call id from its one owner, the call registry."""
    call_id = CallRegistry(max_calls=1, max_seconds=1).reserve()
    assert call_id is not None
    return call_id


def start(office_open: bool = True) -> CallState:
    return new_call(call_id=new_call_id(), started_at=T0, office_open=office_open)


def verified_serving() -> CallState:
    return verify(move(start(), INTAKE), patient_id="P001", first_name="Sarah")


# Each phase reached through legal transitions only, with booking both for a verified caller
# and for a new patient. With the office closed nobody is verified, so serving and hand-off
# are out of reach.
def reached(office_open: bool) -> list[tuple[Phase, CallState]]:
    greeting = start(office_open)
    intake = move(greeting, INTAKE)
    closing = move(intake, CLOSING)
    states = [
        (GREETING, greeting),
        (INTAKE, intake),
        (EMERGENCY, move(intake, EMERGENCY)),
        (CLOSING, closing),
        (ENDED, move(closing, ENDED)),
        (BOOKING, move(intake, BOOKING)),
    ]
    if office_open:
        serving = verify(intake, patient_id="P001", first_name="Sarah")
        states += [
            (SERVING, serving),
            (BOOKING, move(serving, BOOKING)),
            (HANDOFF, move(intake, HANDOFF)),
        ]
    return states


def test_phases_are_the_documented_ones() -> None:
    assert [phase.value for phase in Phase] == [
        "greeting",
        "intake",
        "serving",
        "booking",
        "emergency",
        "handoff",
        "closing",
        "ended",
    ]


# --- R12 ------------------------------------------------------------------------------------

# Every move from a phase (rows) to a phase (columns), written by hand from the spec:
#   x  legal
#   o  legal only while the office is open
#   v  only through the verification transition, and only while the office is open
#   a  only for a verified caller (a new patient's booking never reaches serving)
#   .  refused
# The caller can hang up at any time: every phase but ended moves to ended.
MATRIX = """
            greeting intake serving booking emergency handoff closing ended
greeting       .       x      .       .        x        .       .      x
intake         .       .      v       x        x        o       x      x
serving        .       .      .       x        x        o       x      x
booking        .       .      a       .        x        o       x      x
emergency      .       .      .       .        x        .       .      x
handoff        .       .      .       .        x        .       .      x
closing        .       .      .       .        x        .       .      x
ended          .       .      .       .        .        .       .      .
"""


def parse_matrix() -> dict[tuple[Phase, Phase], str]:
    header, *rows = MATRIX.strip().splitlines()
    targets = [Phase(name) for name in header.split()]
    cells = {}
    for row in rows:
        source, *marks = row.split()
        assert len(marks) == len(targets), row
        for target, mark in zip(targets, marks, strict=True):
            cells[(Phase(source), target)] = mark
    assert len(cells) == len(Phase) ** 2
    return cells


def test_illegal_moves_are_refused() -> None:
    cells = parse_matrix()
    for office_open in (True, False):
        for source, state in reached(office_open):
            for target in Phase:
                mark = cells[(source, target)]
                legal = (
                    mark == "x" or (mark == "o" and office_open) or (mark == "a" and state.verified)
                )
                if legal:
                    assert move(state, target).phase is target, (source, target)
                else:
                    with pytest.raises(IllegalTransitionError):
                        move(state, target)
            # The verification transition is the serving column's other route.
            if cells[(source, SERVING)] == "v" and office_open:
                assert verify(state, patient_id="P002", first_name="Ann").phase is SERVING
            else:
                with pytest.raises(IllegalTransitionError):
                    verify(state, patient_id="P002", first_name="Ann")

    # A refusal names the phases, and the state it was asked of is unchanged.
    intake = move(start(), INTAKE)
    with pytest.raises(IllegalTransitionError, match="intake -> greeting"):
        move(intake, GREETING)
    assert intake.phase is INTAKE

    # Every phase, and both kinds of booking, were tried.
    assert {phase for phase, _ in reached(True)} == set(Phase)
    bookings = [state for phase, state in reached(True) if phase is BOOKING]
    assert sorted(state.verified for state in bookings) == [False, True]

    # A new patient's booking ends some other way than serving.
    new_patient = move(move(start(), INTAKE), BOOKING)
    with pytest.raises(IllegalTransitionError, match="booking -> serving"):
        move(new_patient, SERVING)

    # Ended is final: nothing leaves it.
    ended = dict(reached(True))[ENDED]
    for target in Phase:
        with pytest.raises(IllegalTransitionError):
            move(ended, target)


def test_intake_to_booking_is_a_new_patient() -> None:
    booking = move(move(start(), INTAKE), BOOKING)
    assert booking.is_new_patient is True
    assert booking.verified is False
    assert move(verified_serving(), BOOKING).is_new_patient is False


def test_transitions_as_values_are_the_same_transitions() -> None:
    intake = move(start(), INTAKE)
    assert apply(intake, Move(to=BOOKING)) == move(intake, BOOKING)
    assert apply(intake, Verify(patient_id="P003", first_name="Ann")) == verify(
        intake, patient_id="P003", first_name="Ann"
    )
    with pytest.raises(IllegalTransitionError):
        apply(intake, Move(to=SERVING))


# --- R11 ------------------------------------------------------------------------------------


def every_reachable_state(office_open: bool) -> set[CallState]:
    """Every state a call can reach from its start through any sequence of transitions."""
    steps: list[Callable[[CallState], CallState]] = [
        *[lambda state, target=target: move(state, target) for target in Phase],
        lambda state: verify(state, patient_id="P001", first_name="Sarah"),
        count_tool_error,
        count_refusal,
    ]
    seen = {start(office_open)}
    frontier = list(seen)
    while frontier:
        state = frontier.pop()
        for step in steps:
            try:
                after = step(state)
            except IllegalTransitionError:
                continue
            # The counters grow without bound; two are enough to reach every combination.
            if after.tool_errors <= 2 and after.tool_refusals <= 2 and after not in seen:
                seen.add(after)
                frontier.append(after)
    return seen


def test_verified_only_through_the_transition() -> None:
    for office_open in (True, False):
        states = every_reachable_state(office_open)
        # With the office closed nobody is verified, so serving and hand-off are out of reach.
        assert {state.phase for state in states} == set(Phase) - (
            set() if office_open else {SERVING, HANDOFF}
        )
        for state in states:
            # Every move but the verification one keeps the verification as it was.
            for target in Phase:
                try:
                    after = move(state, target)
                except IllegalTransitionError:
                    continue
                assert (after.verified, after.patient_id, after.patient_first_name) == (
                    state.verified,
                    state.patient_id,
                    state.patient_first_name,
                ), (state.phase, target)
            counted = count_tool_error(state)
            assert (counted.verified, counted.patient_id) == (state.verified, state.patient_id)
            assert counted.tool_errors == state.tool_errors + 1
            refused = count_refusal(state)
            assert (refused.verified, refused.patient_id) == (state.verified, state.patient_id)
            assert refused.tool_refusals == state.tool_refusals + 1
            # A verified state comes only from intake, with the office open.
            if state.verified:
                assert office_open
                assert state.patient_id == "P001"
                assert state.patient_first_name == "Sarah"
            else:
                assert state.patient_id is None
                assert state.patient_first_name is None
        assert any(state.verified for state in states) is office_open

    # The verification transition itself: from intake, office open, a patient id of the
    # declared shape and a first name; anything else is refused.
    intake = move(start(), INTAKE)
    serving = verify(intake, patient_id="P123456", first_name="Ann")
    assert (serving.phase, serving.verified, serving.patient_id, serving.patient_first_name) == (
        SERVING,
        True,
        "P123456",
        "Ann",
    )
    with pytest.raises(IllegalTransitionError, match="office"):
        verify(move(start(office_open=False), INTAKE), patient_id="P001", first_name="Ann")
    nine_digits = "".join(["1" * 3, "2" * 2, "3" * 4])
    for patient_id in ("p001", "P01", "P1234567", nine_digits, "", "P001 "):
        with pytest.raises(ValueError, match="patient_id") as excinfo:
            verify(intake, patient_id=patient_id, first_name="Ann")
        assert patient_id not in str(excinfo.value) or not patient_id
    for first_name in ("", "   "):
        with pytest.raises(ValueError, match="first_name"):
            verify(intake, patient_id="P001", first_name=first_name)

    # A state is checked for consistency whenever it is validated: verified goes with a patient
    # id and a first name, an open office and a phase a verified caller can be in.
    verified = {"verified": True, "patient_id": "P001", "patient_first_name": "Sarah"}
    base = {"call_id": new_call_id(), "started_at": T0, "office_open": True}
    for given in (
        {"verified": True},
        {"patient_id": "P001"},
        {"patient_first_name": "Sarah"},
        {"verified": True, "patient_id": "P001"},
        {"verified": True, "patient_first_name": "Sarah"},
        {**verified, "phase": SERVING, "office_open": False},
        {**verified, "phase": GREETING},
        {**verified, "phase": INTAKE},
        {**verified, "phase": SERVING, "patient_id": "p001"},
        {"phase": SERVING, "tool_refusals": -1},
    ):
        with pytest.raises(ValidationError) as invalid:
            CallState.model_validate({**base, **given})
        assert "Sarah" not in str(invalid.value)
    for phase in (SERVING, BOOKING, EMERGENCY, HANDOFF, CLOSING, ENDED):
        state = CallState.model_validate({**base, **verified, "phase": phase})
        assert (state.verified, state.patient_id) == (True, "P001")

    # The first name never shows in the state's text form.
    assert "Sarah" not in repr(verified_serving())
    assert "Sarah" not in str(verified_serving())


# --- Neighbours -----------------------------------------------------------------------------


def test_state_is_immutable() -> None:
    state = verified_serving()
    with pytest.raises(ValidationError):
        state.verified = False
    with pytest.raises(ValidationError):
        state.phase = EMERGENCY
    assert (state.phase, state.verified) == (SERVING, True)


def test_emergency_from_any_live_phase() -> None:
    for office_open in (True, False):
        for phase, state in reached(office_open):
            if phase is ENDED:
                with pytest.raises(IllegalTransitionError):
                    move(state, EMERGENCY)
            else:
                assert move(state, EMERGENCY).phase is EMERGENCY


def test_a_new_call_starts_at_the_beginning() -> None:
    state = new_call(call_id=new_call_id(), started_at=T0, office_open=True)
    assert (state.phase, state.verified, state.patient_id, state.patient_first_name) == (
        GREETING,
        False,
        None,
        None,
    )
    assert (state.verify_attempts, state.verify_locked, state.tool_errors) == (0, False, 0)
    assert (state.tool_refusals, state.is_new_patient, state.started_at) == (0, False, T0)
    assert new_call(call_id=new_call_id(), started_at=T0, office_open=False).office_open is False


def test_a_verified_state_validates_again() -> None:
    state = verified_serving()
    assert CallState.model_validate(state.model_dump()) == state

    class Holder(BaseModel):
        state: CallState

    assert Holder.model_validate({"state": state.model_dump()}).state == state
    for phase in (BOOKING, EMERGENCY, HANDOFF, CLOSING, ENDED):
        after = move(state, phase)
        assert CallState.model_validate(after.model_dump()) == after


def test_transitions_refuse_an_inconsistent_state() -> None:
    # A state forged past validation (verified with the office closed) is not carried on by any
    # transition: the next state is validated and refused.
    forged = verified_serving().model_copy(update={"office_open": False})
    for target in (BOOKING, CLOSING, EMERGENCY, ENDED):
        with pytest.raises(ValidationError):
            move(forged, target)


def test_a_call_state_checks_what_it_is_given() -> None:
    call_id = new_call_id()
    for bad_id in (call_id.upper(), f"{call_id[:12]}1{call_id[13:]}", "call-1", ""):
        with pytest.raises(ValidationError):
            CallState(call_id=bad_id, started_at=T0, office_open=True)
    with pytest.raises(ValidationError):
        CallState(call_id=call_id, started_at=datetime(2026, 11, 24, 15, 0), office_open=True)
    with pytest.raises(ValidationError):
        CallState(call_id=call_id, started_at=T0)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CallState(call_id=call_id, started_at=T0, office_open="yes")
    with pytest.raises(ValidationError):
        CallState(call_id=call_id, started_at=T0, office_open=True, phase="serving")
    with pytest.raises(ValidationError):
        CallState(call_id=call_id, started_at=T0, office_open=True, tool_errors=-1)
    with pytest.raises(ValidationError):
        CallState(call_id=call_id, started_at=T0, office_open=True, ssn_last4="1234")
