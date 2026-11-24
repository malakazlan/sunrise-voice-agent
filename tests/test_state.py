from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.state import (
    CallState,
    IllegalTransitionError,
    Move,
    Phase,
    Verify,
    apply,
    count_tool_error,
    move,
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
    return CallState(call_id=new_call_id(), started_at=T0, office_open=office_open)


def verified_serving() -> CallState:
    return verify(move(start(), INTAKE), patient_id="P001", first_name="Sarah")


# Each phase reached through legal transitions only.
def reached(office_open: bool) -> dict[Phase, CallState]:
    greeting = start(office_open)
    intake = move(greeting, INTAKE)
    states = {GREETING: greeting, INTAKE: intake, EMERGENCY: move(intake, EMERGENCY)}
    states[CLOSING] = move(intake, CLOSING)
    states[ENDED] = move(states[CLOSING], ENDED)
    if office_open:
        states[SERVING] = verify(intake, patient_id="P001", first_name="Sarah")
        states[BOOKING] = move(states[SERVING], BOOKING)
        states[HANDOFF] = move(intake, HANDOFF)
    else:
        states[BOOKING] = move(intake, BOOKING)
        states[SERVING] = move(states[BOOKING], SERVING)
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
#   .  refused
MATRIX = """
            greeting intake serving booking emergency handoff closing ended
greeting       .       x      .       .        x        .       .      .
intake         .       .      v       x        x        o       x      .
serving        .       .      .       x        x        o       x      .
booking        .       .      x       .        x        o       x      .
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
        for source, state in reached(office_open).items():
            for target in Phase:
                mark = cells[(source, target)]
                legal = mark == "x" or (mark == "o" and office_open)
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
    with pytest.raises(IllegalTransitionError, match="intake -> ended"):
        move(intake, ENDED)
    assert intake.phase is INTAKE

    # Ended is final: nothing leaves it.
    ended = reached(True)[ENDED]
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
            # Tool errors grow without bound; two are enough to reach every combination.
            if after.tool_errors <= 2 and after not in seen:
                seen.add(after)
                frontier.append(after)
    return seen


def test_verified_only_through_the_transition() -> None:
    for office_open in (True, False):
        states = every_reachable_state(office_open)
        assert {state.phase for state in states} >= set(Phase) - (
            set() if office_open else {HANDOFF}
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

    # A call starts unverified: the constructor takes no verification.
    for given in (
        {"verified": True},
        {"patient_id": "P001"},
        {"patient_first_name": "Sarah"},
        {"verified": True, "patient_id": "P001", "patient_first_name": "Sarah"},
    ):
        with pytest.raises(ValidationError) as refused:
            CallState(call_id=new_call_id(), started_at=T0, office_open=True, **given)
        assert "Sarah" not in str(refused.value)

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
        for phase, state in reached(office_open).items():
            if phase is ENDED:
                with pytest.raises(IllegalTransitionError):
                    move(state, EMERGENCY)
            else:
                assert move(state, EMERGENCY).phase is EMERGENCY


def test_a_call_state_checks_what_it_is_given() -> None:
    call_id = new_call_id()
    assert start().phase is GREETING
    assert (start().verify_attempts, start().verify_locked, start().tool_errors) == (0, False, 0)
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
