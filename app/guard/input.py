"""The input guard: what the model may see of a caller's turn, and what code must do about it.

Pure: it decides from the text and the call's state alone, with no database, network or clock,
and changes nothing; the state changes it asks for are in the decision, for its caller to apply
(`apply` in app/agent/state.py). In this order:

1. Emergency. A phrase of the emergency list, negated or not, wins over everything: the model
   sees nothing of the turn, code speaks the emergency line, and the call moves to emergency.
2. The SSN step, only while it is armed. A turn with any digit or number word, written or
   spoken, is swallowed whole and its digits are added to the buffer; a turn without one
   passes. Every
   armed turn is counted. Four digits in all submit them; more than four clear the buffer and
   ask again; the third armed turn submits whatever there is. A submitting decision disarms the
   step and leaves the claim and the buffer for submit_identity, which the caller runs before
   the model sees anything of the turn.
3. SSN redaction, always: digits after a cue, SSN-shaped numbers and any run that reads as
   nine digits. In greeting and intake, while the caller is unverified, every other digit and
   number word too, but for dates, years from 1900 to 2099 and ten- or eleven-digit numbers;
   once verified, any group that is the caller's own last four. The patterns are the mask
   module's (app/guard/mask.py).
4. Flags: an attempt to override the instructions, and a request about someone else. The text
   still passes.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.agent.scripts import ScriptId
from app.agent.state import CallState, Move, Phase, SsnStep
from app.guard.mask import (
    mask_known_digits,
    mask_ssns,
    mask_unverified_digits,
    says_a_number,
    spoken_digits,
)

# Where an unverified caller's numbers are hidden but for dates, years and callback numbers.
_UNVERIFIED_PHASES = frozenset({Phase.GREETING, Phase.INTAKE})

# The digits the SSN step takes, and the armed turns it waits for them.
SSN_DIGITS = 4
MAX_SSN_TURNS = 3

_EMERGENCY = re.compile(
    r"\b(?:chest\s+pain|chest\s+hurts|can'?t\s+breathe|cannot\s+breathe"
    r"|difficulty\s+breathing|trouble\s+breathing|not\s+breathing|suicid\w*|kill\s+myself"
    r"|end\s+my\s+life|overdos\w*|strokes?|heart\s+attack|unconscious|passed\s+out"
    r"|severe\s+bleeding|bleeding\s+heavily)\b",
    re.IGNORECASE,
)
_OVERRIDE = re.compile(
    r"\bignore\s+(?:(?:your|previous|all)\s+)+instructions\b|\bsystem\s+prompt\b"
    r"|\byou\s+are\s+now\b|\bpretend\b|\bread\s+(?:me\s+)?all\b|\bevery\s+patient\b|\bdump\b"
    r"|\blist\s+all\b",
    re.IGNORECASE,
)
_THIRD_PARTY = re.compile(
    r"\bmy\s+(?:wife|husband|mother|father|son|daughter|friend)(?:'s)?\b"
    r"|\bfor\s+someone\s+else\b|\bon\s+behalf\s+of\b",
    re.IGNORECASE,
)


class GuardAction(StrEnum):
    PASS = "pass"  # noqa: S105 (an action, not a password)
    SWALLOW = "swallow"
    EMERGENCY = "emergency"


class Flag(StrEnum):
    INSTRUCTION_OVERRIDE = "instruction_override"
    THIRD_PARTY = "third_party"


class GuardDecision(BaseModel):
    """What to do with a turn: the text the model may see (none unless it passes), the state
    change to apply, a line for code to speak, whether to submit the SSN step, and flags."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    action: GuardAction
    text: str | None = Field(repr=False)
    change: Move | SsnStep | None
    script: ScriptId | None
    submit: bool
    flags: frozenset[Flag]


def _flags(text: str) -> frozenset[Flag]:
    found = set()
    if _OVERRIDE.search(text):
        found.add(Flag.INSTRUCTION_OVERRIDE)
    if _THIRD_PARTY.search(text):
        found.add(Flag.THIRD_PARTY)
    return frozenset(found)


def guard_input(text: str, state: CallState) -> GuardDecision:
    """The decision for one caller turn, from the text and the state alone."""
    plain = text.replace("’", "'")
    if _EMERGENCY.search(plain):
        return GuardDecision(
            action=GuardAction.EMERGENCY,
            text=None,
            change=Move(to=Phase.EMERGENCY),
            script=ScriptId.EMERGENCY,
            submit=False,
            flags=frozenset(),
        )

    change: SsnStep | None = None
    script: ScriptId | None = None
    submit = False
    if state.expecting_ssn4:
        digits = spoken_digits(text)
        turns = state.ssn_turns + 1
        pending = (state.pending_ssn4 or "") + digits
        if len(pending) > SSN_DIGITS:
            pending = ""
            script = ScriptId.SSN_TOO_LONG
        submit = len(pending) == SSN_DIGITS or turns >= MAX_SSN_TURNS
        if submit:
            # submit_identity speaks the outcome.
            script = None
            change = SsnStep(expecting_ssn4=False, ssn_turns=0, pending_ssn4=pending or None)
        else:
            change = SsnStep(expecting_ssn4=True, ssn_turns=turns, pending_ssn4=pending or None)
        if says_a_number(text):
            return GuardDecision(
                action=GuardAction.SWALLOW,
                text=None,
                change=change,
                script=script,
                submit=submit,
                flags=frozenset(),
            )

    seen = mask_ssns(text)
    if not state.verified and state.phase in _UNVERIFIED_PHASES:
        seen = mask_unverified_digits(seen)
    elif state.verified_ssn4 is not None:
        seen = mask_known_digits(seen, state.verified_ssn4)
    return GuardDecision(
        action=GuardAction.PASS,
        text=seen,
        change=change,
        script=script,
        submit=submit,
        flags=_flags(plain),
    )
