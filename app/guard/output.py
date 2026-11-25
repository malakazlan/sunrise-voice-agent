"""The output guard: what the agent may say of the model's own words, a sentence at a time.

Pure: it decides from the text and the call's state alone, with no database, network or clock.
`guard_output` checks one sentence against these rules, in this order, and the first that
matches blocks it with a scripted line to say instead:

1. `ssn_last4`: a run of digits, numerals or number words, equal to the caller's verified last
   four, read across the reply so far. A longer run that holds them, such as a phone number,
   passes.
2. `ssn_shape`: a run that reads as nine digits, in any grouping, verified or not, unless
   those nine are digits the caller said on this call, one after another (`own_digits`: a
   phone number given in pieces, read back before it is whole). The model has no SSN to say:
   the records hold the last four only, and an SSN the caller says is redacted before the
   model. This rule is the last check, and the caller's own number is not what it is for.
3. `phi_outside_gate`: the caller is not verified, or the office is closed, and the sentence
   carries a money amount, a lab status or a dosage.
4. `medical_advice`: advice or diagnosis phrasing.
5. `refill_promise`: a refill, prescription or medication said to be approved, ready or filled,
   unless the sentence asks, waits or denies before it ("whether", "once", "can't" ...).

The digit reading is the mask module's (app/guard/mask.py), the one owner of the SSN patterns;
it reads numerals and number words, not homophones such as "to" or "for". Rules 3 to 5 are
patterns: they cannot catch every phrasing, and they block some harmless sentences. The gate,
which keeps patient data from the model until the caller is verified, is the control; this
guard is the last check before words are said.

A tool may hand the model the caller's own words to read back (a message waiting for
confirmation). Those words, where the reply quotes them (compared without case and repeated
spaces), are not the model's advice or promise: rules 4 and 5 skip them. Rules 1 to 3 read the
whole sentence.

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
from app.guard.mask import nine_digit_readings, says_a_number, says_digits

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
        OutputRule.REFILL_PROMISE: ScriptId.REFILL_NO_PROMISE,
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


def _dosage(text: str) -> bool:
    """A unit with a number, in numerals or words, as the word just before it."""
    for unit in _DOSE_UNIT.finditer(text):
        before = text[max(0, unit.start() - _DOSE_WINDOW) : unit.start()].split()
        if before and says_a_number(before[-1]):
            return True
    return False


def _carries_patient_data(text: str) -> bool:
    return (
        bool(_MONEY.search(text))
        or (bool(_OWING.search(text)) and says_a_number(text))
        or (bool(_LAB_SUBJECT.search(text)) and bool(_LAB_STATUS.search(text)))
        or _dosage(text)
    )


# --- advice and promises ----------------------------------------------------------------

# What "take" may be followed by and not be about a medicine: "take your insurance card".
_TAKE = (
    r"take\b(?!\s+(?:(?:your|a|an|the)\s+)?(?:insurance|photo|id|card|list|copy|moment|seat"
    r"|look|minute|time|message|call|note)\b)"
)
_ADVICE = re.compile(
    "|".join(
        [
            r"\byou\s+(?:should(?:n't|\s+not)?|need\s+to|ought\s+to|must|had\s+better|'d\s+better"
            r"|(?:might|may)\s+want\s+to)\s+(?:(?:also|just|really|probably|definitely|not|never)"
            rf"\s+)?(?:{_TAKE}|stop(?!\s+by\b)|start(?!\s+by\b)|increase|decrease|double(?!-)"
            r"|skip)\b",
            rf"\byou\s+can\s+(?:also\s+|just\s+|still\s+)?{_TAKE}",
            r"\b(?:stop|start|keep|try|continue|avoid|quit)\s+taking\b",
            r"\bhold\s+off\s+(?:on\s+)?taking\b",
            r"\b(?:do\s+not|don't|never)\s+take\b",
            r"\b(?:it|that)(?:'s|\s+is)\s+probably\b(?!\s+(?:best|better|easiest|simplest"
            r"|quickest|fastest|a\s+good\s+idea|in\s+your\s+(?:spam|junk|inbox|email|mail))\b)",
            r"\bit\s+sounds\s+like\s+you(?:'ve|\s+have|\s+(?:may|might)\s+have)\b",
            r"\b(?:it|that|this)\s+sounds\s+like\s+an?\b(?!\s+(?:good|great|nice|plan|question)\b)",
            r"\b(?:it|that|this)\s+could\s+be\s+an?\b(?!\s+(?:few|while|day|week|bit|little"
            r"|couple|moment|minute)\b)",
            r"\byou\s+(?:may|might|probably|likely)\s+have\b(?!\s+(?:to|been|already|just"
            r"|received|heard|seen|gotten|got|left|sent|forgotten|missed|called|tried)\b"
            r"|\s+(?:(?:an?|the|your)\s+)?(?:copay|co-pay|message|voicemail|bill|balance"
            r"|appointment|reminder|call|email|letter|form|text|question|insurance|card)s?\b)",
            r"\b(?:it|that|this)(?:'s|\s+is)\s+(?:(?:perfectly|completely|totally|very|quite"
            r"|pretty)\s+)?normal\b(?!\s+for\s+(?:(?:lab|test)\s+)?results\b|\s+for\s+labs?\b"
            r"|\s+for\s+(?:our|the)\s+(?:lab|office|clinic|staff|team)\b)",
            r"\bnothing\s+to\s+worry\s+about\b",
            r"\bI(?:\s+would|'d)?\s+(?:recommend|suggest)\s+(?:taking|trying(?!\s+(?:to|our)\b))\b",
            r"\bI(?:\s+would|'d)?\s+(?:recommend|suggest)\s+(?:that\s+)?you\s+(?:take|try|stop"
            r"|start|skip|increase|decrease|double)\b",
            r"\b(?:it|that)(?:'s|\s+is)\s+(?:fine|okay|ok|alright|all\s+right|safe)\s+to\s+(?:take"
            r"|skip|stop|double|mix)\b",
            r"\bsafe\s+to\s+take\b",
            r"\bside\s+effects?\s+of\b",
        ]
    ),
    re.IGNORECASE,
)
_REFILL = re.compile(r"\b(?:refills?|refilled|prescriptions?|medications?|meds)\b", re.IGNORECASE)
_PROMISE = re.compile(
    r"\bapproved\b|\bwill\s+be\s+ready\b"
    r"|(?:\b(?:is|are)|'s|'re)\s+(?:now\s+|all\s+)?ready\b(?!\s+to\s+(?:send|submit|go)\b)"
    r"|(?:\bis|'s)\s+all\s+set\b"
    r"|\b(?:has|have|had)\s+(?:now\s+|already\s+)?been\s+(?:re)?filled\b"
    r"|\b(?:was|were)\s+(?:re)?filled\b",
    re.IGNORECASE,
)
# Words that, before a promise word, make it a question, a wait or a denial.
_HEDGE = re.compile(
    r"\b(?:whether|if|once|until|when|after|can't|cannot|can\s+not|don't\s+know"
    r"|do\s+not\s+know|not|(?:must|needs?\s+to)\s+be)\b",
    re.IGNORECASE,
)


def _promises(text: str) -> bool:
    """A refill word, and a promise word that no hedge comes before."""
    if not _REFILL.search(text):
        return False
    promise = _PROMISE.search(text)
    if promise is None:
        return False
    hedge = _HEDGE.search(text)
    return hedge is None or hedge.start() > promise.start()


def _plain(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


def _normal(text: str) -> str:
    """The text without case and with each run of spaces as one."""
    return " ".join(_plain(text).casefold().split())


def _without_read_back(sentence: str, read_back: tuple[str, ...]) -> str:
    """The sentence, compared as `_normal` text, with each read-back text and each of its
    sentences taken out; what is taken out is replaced by a mark no pattern spans."""
    left = _normal(sentence)
    for text in read_back:
        for part in (text, *split_sentences([text])):
            quoted = _normal(part)
            if quoted:
                left = left.replace(quoted, " ¶ ")
    return left


def _broken_rule(
    text: str, state: CallState, said_before: str, read_back: tuple[str, ...], own_digits: str
) -> OutputRule | None:
    so_far = f"{said_before} {text}" if said_before else text
    if state.verified_ssn4 is not None and says_digits(so_far, state.verified_ssn4):
        return OutputRule.SSN_LAST4
    if any(reading not in own_digits for reading in nine_digit_readings(text)):
        return OutputRule.SSN_SHAPE
    if not (state.verified and state.office_open) and _carries_patient_data(text):
        return OutputRule.PHI_OUTSIDE_GATE
    words = _without_read_back(text, read_back) if read_back else text
    if _ADVICE.search(words):
        return OutputRule.MEDICAL_ADVICE
    if _promises(words):
        return OutputRule.REFILL_PROMISE
    return None


def guard_output(
    sentence: str,
    state: CallState,
    *,
    said_before: str = "",
    read_back: tuple[str, ...] = (),
    own_digits: str = "",
) -> Block | None:
    """The block for one sentence of the model's, or None when it may be said. `said_before`
    is what this reply has said already; `read_back` the texts a tool gave the model this turn
    to read back to the caller."""
    rule = _broken_rule(_plain(sentence), state, _plain(said_before), read_back, own_digits)
    return None if rule is None else Block(rule=rule, script=INSTEAD[rule])


# --- sentences --------------------------------------------------------------------------

# What may close a sentence after its marks: quotes and brackets.
_CLOSERS = frozenset("\"')]”’")
# Titles before a name: "Dr. Patel" is one sentence.
_TITLES = frozenset({"dr", "mr", "mrs", "ms", "mx", "prof", "rev"})
# Where the reading is: in text, in a run of marks (. ! ?), in the closers after them, or in
# the space after those, waiting for the next character to decide.
_TEXT, _MARKS, _CLOSING, _AFTER = range(4)


def _ends(asks: bool, word: str, following: str) -> bool:
    """Whether a run of marks ends a sentence, given the first character after its space. "?"
    and "!" always do. A full stop does not before a lower-case letter ("a.m. with"), after a
    title ("Dr. Patel") or after a single capital letter (an initial)."""
    if asks:
        return True
    if following.islower():
        return False
    return word.lower() not in _TITLES and not (len(word) == 1 and word.isupper())


def split_sentences(pieces: Iterable[str]) -> Iterator[str]:
    """Each complete sentence of the text the pieces make, in order, stripped, as soon as it is
    known to be complete; whatever is left is flushed when the pieces end. Empty sentences are
    left out. Each character is read once, however the text is cut into pieces."""
    sentence: list[str] = []
    where = _TEXT
    # The run of marks: whether it holds "?" or "!", and the letters just before it.
    asks = False
    word = ""
    # Where the letters at the end of the sentence so far start.
    letters_from = 0
    for piece in pieces:
        for char in piece:
            if where == _AFTER and not char.isspace():
                if _ends(asks, word, char):
                    done = "".join(sentence).strip()
                    if done:
                        yield done
                    sentence = []
                where = _TEXT
            if char in ".!?":
                if where != _MARKS:
                    after_letter = bool(sentence) and sentence[-1].isalpha()
                    word = "".join(sentence[letters_from:]) if after_letter else ""
                    asks = False
                    where = _MARKS
                asks = asks or char != "."
            elif char in _CLOSERS and where in (_MARKS, _CLOSING):
                where = _CLOSING
            elif char.isspace() and where in (_MARKS, _CLOSING, _AFTER):
                where = _AFTER
            else:
                where = _TEXT
            if char.isalpha() and not (sentence and sentence[-1].isalpha()):
                letters_from = len(sentence)
            sentence.append(char)
    rest = "".join(sentence).strip()
    if rest:
        yield rest


def guard_reply(
    pieces: Iterable[str],
    state: CallState,
    *,
    read_back: tuple[str, ...] = (),
    own_digits: str = "",
) -> GuardedReply:
    """A reply's sentences while each passes `guard_output` and the reply, its sentences joined
    by a space, stays within MAX_REPLY_CHARACTERS. The first blocked sentence ends it: it is not
    said and nothing after it is read. `read_back` is as for `guard_output`."""
    said: list[str] = []
    length = -1
    for sentence in split_sentences(pieces):
        length += 1 + len(sentence)
        if length > MAX_REPLY_CHARACTERS:
            break
        block = guard_output(
            sentence,
            state,
            said_before=" ".join(said),
            read_back=read_back,
            own_digits=own_digits,
        )
        if block is not None:
            return GuardedReply(said=tuple(said), block=block)
        said.append(sentence)
    return GuardedReply(said=tuple(said), block=None)
