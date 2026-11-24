"""The output guard: what the agent may say of the model's own words, a sentence at a time.

Pure: it decides from the text and the call's state alone, with no database, network or clock.
`guard_output` checks one sentence against these rules, in this order, and the first that
matches blocks it with a scripted line to say instead:

1. `ssn_last4`: a run of digits, written or spoken, equal to the caller's verified last four.
   A longer run that holds them, such as a phone number, passes.
2. `ssn_shape`: a run that reads as nine digits, in any grouping, verified or not.
3. `phi_outside_gate`: the caller is not verified, or the office is closed, and the sentence
   carries a money amount, a lab status, a dosage, or refill, prescription or medication with a
   capitalised drug-like word.
4. `medical_advice`: advice or diagnosis phrasing.
5. `refill_promise`: a refill or prescription said to be approved, ready or filled.

The digit reading is the mask module's (app/guard/mask.py), the one owner of the SSN patterns.
Rules 3 to 5 are patterns: they cannot catch every phrasing, and they block some harmless
sentences. The gate, which keeps patient data from the model until the caller is verified, is
the control; this guard is the last check before words are said.

`split_sentences` turns a stream of text pieces into complete sentences, and `guard_reply`
takes a reply through both: its sentences in order while each passes and the reply stays within
MAX_REPLY_CHARACTERS, and the first blocked sentence's line in its place, with nothing after it.
Scripted lines are code's own and never come here.
"""

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from app.agent.scripts import ScriptId
from app.agent.state import CallState
from app.domain.clinic import DOCTORS
from app.guard.mask import mask_known_digits, reads_as_nine_digits, says_a_number

# A reply is cut at the last complete sentence within this many characters.
MAX_REPLY_CHARACTERS = 600


class OutputRule(StrEnum):
    SSN_LAST4 = "ssn_last4"
    SSN_SHAPE = "ssn_shape"
    PHI_OUTSIDE_GATE = "phi_outside_gate"
    MEDICAL_ADVICE = "medical_advice"
    REFILL_PROMISE = "refill_promise"


# Each rule, in the order it is checked, and the line said in place of a sentence it blocks.
INSTEAD = MappingProxyType(
    {
        OutputRule.SSN_LAST4: ScriptId.FALLBACK,
        OutputRule.SSN_SHAPE: ScriptId.FALLBACK,
        OutputRule.PHI_OUTSIDE_GATE: ScriptId.FALLBACK,
        OutputRule.MEDICAL_ADVICE: ScriptId.ADVICE_REFUSAL,
        OutputRule.REFILL_PROMISE: ScriptId.REFILL_SENT,
    }
)


@dataclass(frozen=True, slots=True)
class Block:
    """A blocked sentence: the rule that blocked it and the line to say instead."""

    rule: OutputRule
    script: ScriptId


@dataclass(frozen=True, slots=True)
class GuardedReply:
    """What may be said of a reply: the sentences that passed, in order, and the block that
    ended it, if one did."""

    said: tuple[str, ...] = field(repr=False)
    block: Block | None


# --- patient data -----------------------------------------------------------------------

_MONEY = re.compile(r"[$€£]\s?\d|\b(?:dollars?|cents?|bucks)\b", re.IGNORECASE)
# These words with any number in the sentence are an amount too ("your balance is zero").
_OWING = re.compile(r"\b(?:balance|owe[ds]?|owing)\b", re.IGNORECASE)
_LAB_SUBJECT = re.compile(r"\b(?:labs?|lab\s+work|results?|tests?|blood\s*work)\b", re.IGNORECASE)
_LAB_STATUS = re.compile(
    r"\b(?:ready|pending|available|normal|abnormal|positive|negative|completed?|finished"
    r"|processed|no\s+recent|(?:came|come|are|is)\s+(?:back|in)|back\s+yet"
    r"|doctor\s+will\s+call)\b",
    re.IGNORECASE,
)
_DOSE_UNIT = re.compile(
    r"(?<![^\W\d_])(?:mg|mcg|ml|milligrams?|micrograms?|milliliters?|millilitres?|units?"
    r"|tablets?|pills?|capsules?)(?![^\W\d_])",
    re.IGNORECASE,
)
# How far before a unit its number is looked for: the longest number word, with room.
_DOSE_WINDOW = 32
_MEDICATION_WORD = re.compile(
    r"\b(?:refills?|refilled|prescriptions?|prescribed|medications?|medicines?|meds)\b",
    re.IGNORECASE,
)
_CAPITALISED = re.compile(r"(?<![\w'])[A-Z][A-Za-z-]{2,}")
_FIRST_LETTER = re.compile(r"[^\W\d_]")
# Capitalised words that are never a drug: the clinic's own words, its doctors, days and months.
_NOT_DRUGS = frozenset(
    "sunrise family clinic doctor social security eastern time okay mrs".split()
    + "monday tuesday wednesday thursday friday saturday sunday".split()
    + "january february march april may june july august september october november".split()
    + ["december"]
    + [doctor.split()[-1].lower() for doctor in DOCTORS]
)


def _dosage(text: str) -> bool:
    """A unit with a number, in numerals or words, as the word just before it."""
    for unit in _DOSE_UNIT.finditer(text):
        before = text[max(0, unit.start() - _DOSE_WINDOW) : unit.start()].split()
        if before and says_a_number(before[-1]):
            return True
    return False


def _names_a_drug(text: str) -> bool:
    """A capitalised word, not the sentence's first, that is not one of the clinic's words."""
    first = _FIRST_LETTER.search(text)
    for word in _CAPITALISED.finditer(text):
        if first is not None and word.start() == first.start():
            continue
        if word[0].lower() not in _NOT_DRUGS:
            return True
    return False


def _carries_patient_data(text: str) -> bool:
    return (
        bool(_MONEY.search(text))
        or (bool(_OWING.search(text)) and says_a_number(text))
        or (bool(_LAB_SUBJECT.search(text)) and bool(_LAB_STATUS.search(text)))
        or _dosage(text)
        or (bool(_MEDICATION_WORD.search(text)) and _names_a_drug(text))
    )


# --- advice and promises ----------------------------------------------------------------

_ADVICE = re.compile(
    "|".join(
        [
            r"\byou\s+(?:should(?:n't|\s+not)?|need\s+to|ought\s+to|must|had\s+better|'d\s+better"
            r"|(?:might|may)\s+want\s+to)\s+(?:(?:also|just|really|probably|definitely|not|never)"
            r"\s+)?(?:take|stop(?!\s+by\b)|start(?!\s+by\b)|increase|decrease|double|skip)\b",
            r"\b(?:stop|start|keep|try|continue|avoid|quit)\s+taking\b",
            r"\b(?:do\s+not|don't|never)\s+take\b",
            r"\b(?:it|that)(?:'s|\s+is)\s+probably\b(?!\s+(?:best|better|easiest|simplest"
            r"|quickest|fastest|a\s+good\s+idea)\b)",
            r"\bit\s+sounds\s+like\s+you(?:'ve|\s+have|\s+(?:may|might)\s+have)\b",
            r"\byou\s+(?:may|might|probably)\s+have\b(?!\s+(?:to|been|already|just|received"
            r"|heard|seen|gotten|got|left|sent|forgotten|missed|called|tried)\b)",
            r"\b(?:it|that|this)(?:'s|\s+is)\s+(?:(?:perfectly|completely|totally|very|quite"
            r"|pretty)\s+)?normal\b",
            r"\bnothing\s+to\s+worry\s+about\b",
            r"\bI(?:\s+would|'d)?\s+(?:recommend|suggest)\s+(?:taking|trying(?!\s+to\b))\b",
            r"\bsafe\s+to\s+take\b",
            r"\bside\s+effects?\s+of\b",
        ]
    ),
    re.IGNORECASE,
)
_REFILL = re.compile(r"\b(?:refills?|prescriptions?)\b", re.IGNORECASE)
_PROMISE = re.compile(
    r"\bapproved\b|(?:\b(?:is|are)|'s|'re)\s+(?:now\s+|all\s+)?ready\b"
    r"|\b(?:has|have|had)\s+(?:now\s+|already\s+)?been\s+filled\b|\b(?:was|were)\s+filled\b",
    re.IGNORECASE,
)


def _broken_rule(text: str, state: CallState) -> OutputRule | None:
    if state.verified_ssn4 is not None and mask_known_digits(text, state.verified_ssn4) != text:
        return OutputRule.SSN_LAST4
    if reads_as_nine_digits(text):
        return OutputRule.SSN_SHAPE
    if not (state.verified and state.office_open) and _carries_patient_data(text):
        return OutputRule.PHI_OUTSIDE_GATE
    if _ADVICE.search(text):
        return OutputRule.MEDICAL_ADVICE
    if _REFILL.search(text) and _PROMISE.search(text):
        return OutputRule.REFILL_PROMISE
    return None


def guard_output(sentence: str, state: CallState) -> Block | None:
    """The block for one sentence of the model's, or None when it may be said."""
    rule = _broken_rule(sentence.replace("’", "'").replace("‘", "'"), state)
    return None if rule is None else Block(rule=rule, script=INSTEAD[rule])


# --- sentences --------------------------------------------------------------------------

# A sentence's end: its marks, any closing quote or bracket, and the space after them. Whether
# it ends the sentence is decided once the next character is known. A match starts only where
# a run of marks starts, and takes the run whole, so a long run is read once.
_END = re.compile(r"(?<![.!?])[.!?]++[\"')\]”’]*+\s+")
_END_CHARACTERS = frozenset(".!?\"')]”’")
# Titles before a name: "Dr. Patel" is one sentence.
_TITLES = frozenset({"dr", "mr", "mrs", "ms", "mx", "prof", "rev"})


def _ends_sentence(text: str, end: re.Match[str]) -> bool:
    """Whether the marks end a sentence, given the character after their space. "?" and "!"
    always do. A full stop does not before a lower-case letter ("a.m. with"), after a title
    ("Dr. Patel") or after a single capital letter (an initial)."""
    if "?" in end[0] or "!" in end[0]:
        return True
    if text[end.end()].islower():
        return False
    start = end.start()
    while start and text[start - 1].isalpha():
        start -= 1
    word = text[start : end.start()]
    return word.lower() not in _TITLES and not (len(word) == 1 and word.isupper())


def split_sentences(pieces: Iterable[str]) -> Iterator[str]:
    """Each complete sentence of the text the pieces make, in order, stripped, as soon as it is
    known to be complete; whatever is left is flushed when the pieces end. Empty sentences are
    left out."""
    pending = ""
    start = 0
    # Where the search for the next end starts: no end can begin before it.
    position = 0
    for piece in pieces:
        pending = pending[start:] + piece
        position -= start
        start = 0
        while (end := _END.search(pending, position)) is not None:
            if end.end() == len(pending):
                # The character after the space has not come yet.
                position = end.start()
                break
            if _ends_sentence(pending, end):
                sentence = pending[start : end.end()].strip()
                if sentence:
                    yield sentence
                start = end.end()
            position = end.end()
        else:
            # Marks at the very end may start an end once more text comes.
            position = len(pending)
            while position > start and pending[position - 1] in _END_CHARACTERS:
                position -= 1
    rest = pending[start:].strip()
    if rest:
        yield rest


def guard_reply(pieces: Iterable[str], state: CallState) -> GuardedReply:
    """A reply's sentences while each passes `guard_output` and the reply, its sentences joined
    by a space, stays within MAX_REPLY_CHARACTERS. The first blocked sentence ends it: it is not
    said and nothing after it is read."""
    said: list[str] = []
    length = -1
    for sentence in split_sentences(pieces):
        length += 1 + len(sentence)
        if length > MAX_REPLY_CHARACTERS:
            break
        block = guard_output(sentence, state)
        if block is not None:
            return GuardedReply(said=tuple(said), block=block)
        said.append(sentence)
    return GuardedReply(said=tuple(said), block=None)
