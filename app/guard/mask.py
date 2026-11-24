"""Masks identifiers in free text: SSNs, dates, phone numbers and email addresses.

Text is masked line by line, each line in one pass of one pattern, so the time taken grows with
the length of the text: every alternative starts only where its lookbehind allows and has only
bounded repeats, or possessive ones over a run that is scanned once. Masking is idempotent by
construction: no alternative can consume a "*", start just after "*", "*-" or "*.", or end just
before "*" or a letter or digit, so nothing a replacement writes or keeps can be part of a new
match. Digits of any script count as digits; words and separators are matched in ASCII.
"""

import re

MAX_LENGTH = 10_000
CUT_MARKER = " [cut]"

# Where a match may start: not inside a run of letters or digits (so UUIDs, P001 and hex ids
# are left alone), and not just after masked output.
_START = r"(?<![^\W_])(?<!\*)(?<!\*[-.])"
# The same, but letters may come just before (dob1985-03-14).
_START_AFTER_LETTERS = r"(?<!\d)(?<!\*)(?<!\*[-.])"
# Where a match may end: not inside a run of letters or digits, and not just before "*".
_END = r"(?![^\W_])(?!\*)"

_UNIT = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)(?![a-z])"
_NUMBER = (
    r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen"
    r"|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty"
    r"|seventy|eighty|ninety|hundred|double|triple)(?![a-z])"
)
_ORDINAL_WORDS = (
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth"
    " thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth"
).split()
_ORDINALS = {word: number for number, word in enumerate(_ORDINAL_WORDS, start=1)}
_TENS = {"twenty": 20, "thirty": 30, "twentieth": 20, "thirtieth": 30}
_ORDINAL = rf"(?:(?:twenty|thirty)[ -])?(?:{'|'.join(_ORDINAL_WORDS)})|twentieth|thirtieth"
_UNIT_DIGITS = {"zero": "0", "oh": "0"} | {
    word: str(number)
    for number, word in enumerate("one two three four five six seven eight nine".split(), 1)
}
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?(?![a-z])"
)


def _day(name: str) -> str:
    return rf"(?:(?P<{name}_day>\d{{1,2}})(?:st|nd|rd|th)?|(?P<{name}_ordinal>{_ORDINAL}))(?![a-z])"


# An SSN cue, and the short words that may stand between a cue and the digits ("security
# number is"). A cue may follow "_" (ssn_last4).
_CUE = r"(?:ssn|social|last[_ \t-]?(?:four|4)|ending[ \t]+in|ends[ \t]+in)(?![a-z])"
_GAP = r"[\s:;,.#=\"'()\[\]-]{0,8}+"
# A word between is never a number, and never the start of another cue: the scan starts again
# at that cue, so the "four" of "last four" is never taken for the digits.
_FILLER = rf"(?!{_CUE})(?!{_NUMBER})[a-z]{{1,20}}+(?![a-z])"
# Up to twelve digits or number words.
_DIGITS = rf"(?:\d|{_NUMBER})(?:[\s,.-]{{0,3}}(?:\d|{_NUMBER})){{0,11}}"
_PHONE_SEPARATOR = r"[ ./-]{0,2}"
_WORD_SEPARATOR = r"[ \t,-]{1,3}"

_IDENTIFIER = re.compile(
    "|".join(
        [
            r"(?P<email>(?<![\w.%+*-])(?P<email_first>[\w.%+-])[\w.%+-]*+@"
            r"(?:[a-z0-9-]++\.(?=[a-z0-9-]))++(?P<email_tld>[a-z]{2,}+)(?![\w*-]))",
            rf"(?P<cue>(?P<cue_kept>{_START}{_CUE}(?:{_GAP}{_FILLER}){{0,8}}{_GAP}){_DIGITS}{_END})",
            # Python's repr of a date, or of a date and time (whose time is kept).
            rf"(?P<repr>{_START}datetime\.date(?:"
            rf"time\(\d{{4}}, \d{{1,2}}, (?P<repr_time_day>\d{{1,2}})(?=[,)])"
            rf"|\(\d{{4}}, \d{{1,2}}, (?P<repr_day>\d{{1,2}})\)))",
            rf"(?P<ssn>{_START}(?:\d{{3}}[- .]\d{{2}}[- .]\d{{4}}|\d{{9}}){_END})",
            rf"(?P<phone>(?:\+1{_PHONE_SEPARATOR}|(?=\()|{_START}(?:1{_PHONE_SEPARATOR})?)"
            rf"(?:\(\d{{3}}\){_PHONE_SEPARATOR}|\d{{3}}{_PHONE_SEPARATOR})\d{{3}}{_PHONE_SEPARATOR}"
            rf"(?P<phone_last>\d{{4}}){_END})",
            rf"(?P<phone_words>{_START}{_UNIT}(?:{_WORD_SEPARATOR}{_UNIT}){{5}}"
            + "".join(f"{_WORD_SEPARATOR}(?P<phone_word{n}>{_UNIT})" for n in range(4))
            + rf"{_END})",
            # Year, month, day; an ISO date may be followed by the T of a date and time.
            rf"(?P<ymd>{_START_AFTER_LETTERS}\d{{4}}[-/]\d{{1,2}}[-/](?P<ymd_day>\d{{1,2}})"
            r"(?![^\W_T])(?!\*))",
            # Month, day, year.
            rf"(?P<mdy>{_START}\d{{1,2}}[/-](?P<mdy_day>\d{{1,2}})[/-](?:\d{{4}}|\d{{2}}){_END})",
            # Day, month, year, with dots.
            rf"(?P<dmy>{_START}(?P<dmy_day>\d{{1,2}})\.\d{{1,2}}\.\d{{4}}{_END})",
            rf"(?P<month_first>{_START}{_MONTH}[ \t]{{1,3}}{_day('month_first')}"
            rf"(?:,[ \t]{{0,3}}|[ \t]{{1,3}})\d{{4}}{_END})",
            rf"(?P<day_first>{_START}{_day('day_first')}(?:[ \t]{{1,3}}(?:of[ \t]{{1,3}})?|-)"
            rf"{_MONTH}(?:,[ \t]{{0,3}}|[ \t]{{1,3}}|-)(?:\d{{4}}|\d{{2}}(?!:)){_END})",
        ]
    ),
    re.IGNORECASE,
)

_DAY_GROUPS = (
    "repr_day",
    "repr_time_day",
    "ymd_day",
    "mdy_day",
    "dmy_day",
    "month_first_day",
    "month_first_ordinal",
    "day_first_day",
    "day_first_ordinal",
)


def _day_number(text: str) -> str:
    if text[0].isdigit():
        return text.zfill(2)
    words = re.split(r"[ -]", text.lower())
    if len(words) == 2:
        return str(_TENS[words[0]] + _ORDINALS[words[1]])
    return f"{_ORDINALS.get(words[0]) or _TENS[words[0]]:02d}"


def _replace(match: re.Match[str]) -> str:
    kind = match.lastgroup
    if kind == "email":
        return f"{match['email_first']}***@***.{match['email_tld']}"
    if kind == "cue":
        return f"{match['cue_kept']}****"
    if kind == "ssn":
        return "***-**-****"
    if kind == "phone":
        return f"***-***-{match['phone_last']}"
    if kind == "phone_words":
        return "***-***-" + "".join(_UNIT_DIGITS[match[f"phone_word{n}"].lower()] for n in range(4))
    day = next(match[name] for name in _DAY_GROUPS if match[name] is not None)
    return f"****-**-{_day_number(day)}"


def _mask_line(line: str) -> str:
    if len(line) > MAX_LENGTH:
        line = line[: MAX_LENGTH - len(CUT_MARKER)] + CUT_MARKER
    masked = _IDENTIFIER.sub(_replace, line)
    if len(masked) <= MAX_LENGTH:
        return masked
    # Masking made the line longer. It is cut again, before any run of letters or digits that
    # the cut would split, so that no match can end at the cut.
    end = MAX_LENGTH - len(CUT_MARKER)
    while end and masked[end - 1].isalnum():
        end -= 1
    return masked[:end] + CUT_MARKER


def mask(text: str) -> str:
    """The text with every identifier masked, line by line. A line longer than MAX_LENGTH is
    cut to that length, the marker included, before matching. mask(mask(text)) == mask(text)."""
    return "\n".join(_mask_line(line) for line in text.split("\n"))
