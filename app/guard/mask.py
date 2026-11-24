"""Masks identifiers in free text: SSNs, dates, phone numbers and email addresses.

Every pattern is bounded or possessive and starts only where a match can begin, so the time
taken grows with the length of the text and no input makes a pattern backtrack over it.
Digits of any script count as digits; words and separators are matched in ASCII.
"""

import re
from collections.abc import Callable

MAX_LENGTH = 10_000
CUT_MARKER = " [cut]"

# A match never starts or ends inside a run of letters or digits, so ids such as UUIDs, P001 or
# hex revision ids are left alone.
_START = r"(?<![^\W_])"
_END = r"(?![^\W_])"

_EMAIL = re.compile(
    r"(?<![\w.%+-])([\w.%+-])[\w.%+-]*+@(?:[a-z0-9-]++\.(?=[a-z0-9-]))++([a-z]{2,}+)(?![\w-])",
    re.IGNORECASE,
)
_SSN_GROUPED = re.compile(rf"{_START}\d{{3}}[- ]\d{{2}}[- ]\d{{4}}{_END}")
_SSN_PLAIN = re.compile(rf"{_START}\d{{9}}{_END}")
# "+1 ", or a "(" opening the area code, or the start of a run of digits with an optional "1 ".
_PHONE = re.compile(
    rf"(?:\+1[ .-]?|(?=\()|{_START}(?:1[ .-]?)?)"
    rf"(?:\(\d{{3}}\)[ .-]?|\d{{3}}[ .-]?)\d{{3}}[ .-]?(\d{{4}}){_END}"
)
# An ISO date may be followed by the T of a date and time.
_ISO_DATE = re.compile(rf"{_START}\d{{4}}-\d{{2}}-(\d{{2}})(?![^\W_T])")
# Month, day, year.
_NUMERIC_DATE = re.compile(rf"{_START}\d{{1,2}}[/-](\d{{1,2}})[/-](?:\d{{4}}|\d{{2}}){_END}")
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_SPOKEN_DATE_MONTH_FIRST = re.compile(
    rf"\b{_MONTH}[ \t]++{_DAY},?[ \t]++\d{{4}}{_END}", re.IGNORECASE
)
_SPOKEN_DATE_DAY_FIRST = re.compile(
    rf"{_START}{_DAY}[ \t]++(?:of[ \t]++)?{_MONTH},?[ \t]++\d{{4}}{_END}", re.IGNORECASE
)
# After a cue, up to six short words ("security number is"), then up to nine digits, spoken
# or written. A digit word is never taken as one of the words in between.
_DIGIT_WORD = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)(?![a-z])"
_DIGIT = rf"(?:\d|{_DIGIT_WORD})"
_GAP = r"[\s:;,.#=\"'-]{1,8}+"
_WORD = rf"(?!{_DIGIT_WORD})[a-z]{{1,20}}+(?![a-z])"
_SSN_CUE = re.compile(
    rf"(\b(?:ssn|social|last[ \t]++(?:four|4))\b(?:{_GAP}{_WORD}){{0,6}}{_GAP})"
    rf"{_DIGIT}(?:[\s,.-]{{0,3}}{_DIGIT}){{0,8}}(?!\d)",
    re.IGNORECASE,
)


def _date(match: re.Match[str]) -> str:
    return f"****-**-{match[1].zfill(2)}"


_RULES: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (_EMAIL, lambda match: f"{match[1]}***@***.{match[2]}"),
    (_SSN_GROUPED, "***-**-****"),
    (_SSN_PLAIN, "***-**-****"),
    (_PHONE, lambda match: f"***-***-{match[1]}"),
    (_ISO_DATE, _date),
    (_NUMERIC_DATE, _date),
    (_SPOKEN_DATE_MONTH_FIRST, _date),
    (_SPOKEN_DATE_DAY_FIRST, _date),
    (_SSN_CUE, r"\1****"),
)


def mask(text: str) -> str:
    """The text with every identifier masked. Text longer than MAX_LENGTH is cut to that length,
    the marker included, before matching. The rules run until nothing changes, and the result
    is cut again if masking made it longer, so that mask(mask(text)) == mask(text)."""
    while True:
        if len(text) > MAX_LENGTH:
            text = text[: MAX_LENGTH - len(CUT_MARKER)] + CUT_MARKER
        masked = text
        for pattern, replacement in _RULES:
            masked = pattern.sub(replacement, masked)
        if masked == text:
            return text
        text = masked
