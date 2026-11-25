"""Masks identifiers in free text: SSNs, dates, phone numbers and email addresses.

Text is masked line by line, each line in one pass of one pattern and then one pass that reads
runs of digits and number words, masking any run that reads as exactly nine digits as an SSN;
so the time taken grows with the length of the text: every alternative starts only where its
lookbehind allows and has only bounded repeats, or possessive ones over a run that is scanned
once. Masking is idempotent by construction: no alternative can consume a "*", start just after "*",
"*-" or "*.", or end just before "*" or a letter or digit, so nothing a replacement writes or
keeps can be part of a new match; a nine-digit run is replaced by text with no digit. Digits
of any script count as digits; words and separators are matched in ASCII.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

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
_LAST_FOUR = r"last[_ \t-]?(?:four|4)"
_CUE = rf"(?:ssn|social|{_LAST_FOUR}|ending[ \t]+in|ends[ \t]+in)(?![a-z])"
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
    masked = _mask_nine(_IDENTIFIER.sub(_replace, line))
    if len(masked) <= MAX_LENGTH:
        return masked
    # Masking made the line longer. It is cut again, before any run of digits and number words
    # and any run of letters or digits that the cut would split, so that no match can end at
    # the cut and no run is left shorter than it was read.
    end = MAX_LENGTH - len(CUT_MARKER)
    for run in _runs(masked):
        if run[0].start < end < run[-1].end:
            end = run[0].start
            break
    while end and masked[end - 1].isalnum():
        end -= 1
    return masked[:end] + CUT_MARKER


def mask(text: str) -> str:
    """The text with every identifier masked, line by line. A line longer than MAX_LENGTH is
    cut to that length, the marker included, before matching. mask(mask(text)) == mask(text)."""
    return "\n".join(_mask_line(line) for line in text.split("\n"))


_SSN_KINDS = frozenset({"ssn", "cue"})


def _replace_ssn(match: re.Match[str]) -> str:
    return _replace(match) if match.lastgroup in _SSN_KINDS else match[0]


def mask_ssns(text: str) -> str:
    """The text with SSNs masked as `mask` masks them (a grouped or nine-digit number, digits
    after a cue, a run of tokens that reads as nine digits), line by line; every other
    identifier is kept as it is. For text that is stored for people to read, such as a
    caller's message. Not cut."""
    return "\n".join(_mask_nine(_IDENTIFIER.sub(_replace_ssn, line)) for line in text.split("\n"))


# Digits as said or written, read in runs. A token is a run of numerals or a number word: a unit
# (zero, oh, one to nine), a teen, a tens word, hundred, thousand, double or triple, or a
# homophone of a digit (won, to, too, tree, for, fore, ate). Tokens join into one run across
# up to three spaces, hyphens or dots, or the word "dash"; across a comma only between two
# single digits ("three, three, four"). Within a run: a teen is two digits; a tens word is two,
# with the unit after it if there is one; hundred after a one- or two-digit number adds two
# zeros; double and triple repeat the next digit. A homophone is a digit only in a run with a
# numeral, a unit, a teen or a tens word, or with another homophone; "oh" is a zero only with
# one of those or a homophone that is a digit. The "four" of the cue "last four" is never a
# digit. Every step is one pass over the text.
_TEEN_DIGITS = {
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
}
_TENS_DIGIT = {
    "twenty": "2",
    "thirty": "3",
    "forty": "4",
    "fifty": "5",
    "sixty": "6",
    "seventy": "7",
    "eighty": "8",
    "ninety": "9",
}
_HOMOPHONE_DIGITS = {
    "won": "1",
    "to": "2",
    "too": "2",
    "tree": "3",
    "for": "4",
    "fore": "4",
    "ate": "8",
}
_SCALES = ("hundred", "thousand")
_REPEATS = {"double": 2, "triple": 3}
_NUMBER_WORDS = sorted(
    [*_UNIT_DIGITS, *_TEEN_DIGITS, *_TENS_DIGIT, *_HOMOPHONE_DIGITS, *_SCALES, *_REPEATS],
    key=len,
    reverse=True,
)
_DIGIT_TOKEN = re.compile(
    rf"\d+|(?<![^\W\d_])(?:{'|'.join(_NUMBER_WORDS)})(?![^\W\d_])", re.IGNORECASE
)
_JOINS = re.compile(r"[ \t.-]{0,3}|[ \t]{0,3}dash[ \t]{0,3}", re.IGNORECASE)
_JOINS_SINGLE_DIGITS = re.compile(r"[ \t-]{0,2},[ \t-]{0,2}")
# What the output guard reads (`says_digits`, `reads_as_nine_digits`): numerals and number
# words without the homophones, so "ten thirty to eleven thirty" is two times. For the caller's
# last four, tokens also join across commas, brackets, en and em dashes, "and", and um or uh.
_SPOKEN_TOKEN = re.compile(
    rf"\d+|(?<![^\W\d_])(?:{'|'.join(w for w in _NUMBER_WORDS if w not in _HOMOPHONE_DIGITS)})"
    r"(?![^\W\d_])",
    re.IGNORECASE,
)
_LOOSE_JOINS = re.compile(r"(?:[ \t.,()–—-]|dash|and|um|uh){0,12}", re.IGNORECASE)
_FILLER_WORD = re.compile(r"and|um|uh", re.IGNORECASE)
_LAST_FOUR_CUE = re.compile(rf"(?<![^\W\d_]){_LAST_FOUR}(?![^\W\d_])", re.IGNORECASE)
# Tokens a run may be trimmed of at either end when it is read as a group: a homophone or an
# "oh" just before or after the digits ("say it to ...").
_SOFT = frozenset({*_HOMOPHONE_DIGITS, "oh"})

# The dates `mask` recognises, and years from 1900 to 2099 in words.
_DATE_KINDS = frozenset({"repr", "ymd", "mdy", "dmy", "month_first", "day_first"})
_SPACE = r"[ \t-]{1,3}"
_UNIT_WORD = "one|two|three|four|five|six|seven|eight|nine"
_TEEN_WORD = "|".join(sorted(_TEEN_DIGITS, key=len, reverse=True))
_TENS_WORD = "|".join(_TENS_DIGIT)
_YEAR_END = (
    rf"(?:(?:{_TENS_WORD})(?:{_SPACE}(?:{_UNIT_WORD}))?|{_TEEN_WORD}|oh{_SPACE}(?:{_UNIT_WORD}))"
)
_YEAR_WORDS = re.compile(
    rf"(?<![^\W\d_])(?:(?:nineteen|twenty){_SPACE}{_YEAR_END}|nineteen{_SPACE}hundred"
    rf"|two{_SPACE}thousand(?:{_SPACE}(?:and{_SPACE})?(?:{_YEAR_END}|{_UNIT_WORD}))?)"
    r"(?![^\W\d_])",
    re.IGNORECASE,
)
_YEARS = range(1900, 2100)
# Runs this long are telephone numbers: a caller gives one to be called back.
_CALLBACK_LENGTHS = (10, 11)


@dataclass(frozen=True, slots=True)
class _Token:
    start: int
    end: int
    word: str
    # A digit or a number word: what the SSN step swallows and redaction hides.
    counted: bool
    # The digits it adds to its run's reading.
    reading: str


def _single(word: str) -> bool:
    return (
        (word[0].isdigit() and len(word) == 1) or word in _UNIT_DIGITS or word in _HOMOPHONE_DIGITS
    )


def _read(matches: list[re.Match[str]]) -> list[_Token]:
    words = [match[0].lower() for match in matches]
    base = any(
        word[0].isdigit()
        or word in _TEEN_DIGITS
        or word in _TENS_DIGIT
        or (word in _UNIT_DIGITS and word != "oh")
        for word in words
    )
    homophones = sum(word in _HOMOPHONE_DIGITS for word in words)
    sounds = base or homophones > 1
    zero = base or (sounds and homophones > 0)
    readings: list[tuple[bool, str]] = []
    repeat = 1
    last = 0
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word in _REPEATS:
            repeat = _REPEATS[word]
            readings.append((True, ""))
            continue
        if word in _SCALES:
            readings.append((True, "00" if word == "hundred" and last in (1, 2) else ""))
            last = 0
            continue
        if (word in _HOMOPHONE_DIGITS and not sounds) or (word == "oh" and not zero):
            readings.append((False, ""))
            repeat = 1
            continue
        joined = False
        if word[0].isdigit():
            read = "".join(str(int(char)) for char in word)
        elif word in _TEEN_DIGITS:
            read = _TEEN_DIGITS[word]
        elif word in _TENS_DIGIT:
            unit = words[index] if index < len(words) else ""
            joined = unit in _UNIT_DIGITS and unit not in ("zero", "oh")
            read = _TENS_DIGIT[word] + (_UNIT_DIGITS[unit] if joined else "0")
        elif word in _HOMOPHONE_DIGITS:
            read = _HOMOPHONE_DIGITS[word]
        else:
            read = _UNIT_DIGITS[word]
        readings.append((True, read[0] * repeat + read[1:]))
        if joined:
            readings.append((True, ""))
            index += 1
        repeat = 1
        last = len(read)
    return [
        _Token(match.start(), match.end(), match[0].lower(), counted, reading)
        for match, (counted, reading) in zip(matches, readings, strict=True)
    ]


def _runs(
    text: str, *, tokens: re.Pattern[str] = _DIGIT_TOKEN, loose: bool = False
) -> list[list[_Token]]:
    """Each run of tokens in the text, read; with `loose`, joined across _LOOSE_JOINS too."""
    text = _LAST_FOUR_CUE.sub(lambda cue: " " * len(cue[0]), text)
    runs: list[list[re.Match[str]]] = []
    for token in tokens.finditer(text):
        if runs:
            previous = runs[-1][-1]
            gap = text[previous.end() : token.start()]
            if (
                _JOINS.fullmatch(gap)
                or (loose and _LOOSE_JOINS.fullmatch(gap))
                or (
                    _JOINS_SINGLE_DIGITS.fullmatch(gap)
                    and _single(previous[0].lower())
                    and _single(token[0].lower())
                )
            ):
                runs[-1].append(token)
                continue
        runs.append([token])
    return [_read(run) for run in runs]


def _digits(tokens: list[_Token]) -> str:
    return "".join(token.reading for token in tokens)


def _groups(run: list[_Token]) -> list[list[_Token]]:
    """The run, and the run without the homophones and "oh" at its ends, when that differs."""
    start, end = 0, len(run)
    while start < end and run[start].word in _SOFT:
        start += 1
    while end > start and run[end - 1].word in _SOFT:
        end -= 1
    trimmed = run[start:end]
    return [run] if len(trimmed) == len(run) or not trimmed else [run, trimmed]


def _replaced(line: str, spans: list[tuple[int, int, str]]) -> str:
    """The line with each span, in order and not overlapping, replaced."""
    pieces = []
    kept = 0
    for start, end, replacement in spans:
        pieces.append(line[kept:start] + replacement)
        kept = end
    return "".join(pieces) + line[kept:]


def _ssn_run_may_start(line: str, start: int) -> bool:
    return not (start and line[start - 1].isalnum())


def _ssn_run_may_end(line: str, end: int) -> bool:
    return not (end < len(line) and line[end].isalnum())


def _mask_nine(line: str) -> str:
    """The line with every run that reads as exactly nine digits masked as an SSN. A run that
    starts or ends inside letters or digits is part of an id and is left. What it writes holds
    no digit, and a run it leaves reads the same the next time, so masking stays idempotent."""
    spans = []
    for run in _runs(line):
        for group in _groups(run):
            start, end = group[0].start, group[-1].end
            if (
                len(_digits(group)) == 9
                and _ssn_run_may_start(line, start)
                and _ssn_run_may_end(line, end)
            ):
                spans.append((start, end, "***-**-****"))
                break
    return _replaced(line, spans)


def spoken_digits(text: str) -> str:
    """Every digit the text says, in order, written or spoken."""
    return "".join(_digits(run) for run in _runs(text))


def says_a_number(text: str) -> bool:
    """Whether the text holds any digit or number word, whether or not it reads as digits."""
    return any(token.counted for run in _runs(text) for token in run)


def reads_as_nine_digits(text: str) -> bool:
    """For the output guard: whether any run of numerals and number words (no homophones), or
    the run without an "oh" at its ends, reads as exactly nine digits, line by line; what
    `mask` hides as an SSN, here wherever the run starts or ends, inside letters or digits
    too."""
    return any(
        len(_digits(group)) == 9
        for line in text.split("\n")
        for run in _runs(line, tokens=_SPOKEN_TOKEN)
        for group in _groups(run)
    )


def _says_in_run(line: str, run: list[_Token], digits: str) -> bool:
    """Whether some tokens of the run, one after another, read exactly `digits`. A run that
    reads as a callback number, joined by separators only (no "and", um or uh), is a phone
    number and says none of its parts."""
    readings = [token.reading for token in run]
    read = "".join(readings)
    joined_by_fillers = any(
        _FILLER_WORD.search(line[before.end : after.start])
        for before, after in zip(run, run[1:], strict=False)
    )
    if len(read) in _CALLBACK_LENGTHS and not joined_by_fillers:
        return False
    # Where each token's reading starts and ends in the run's.
    starts = {0}
    total = 0
    for reading in readings:
        total += len(reading)
        starts.add(total)
    return any(read[at : at + len(digits)] == digits for at in starts if at + len(digits) in starts)


def says_digits(text: str, digits: str) -> bool:
    """For the output guard: whether the text says `digits`, line by line: some tokens of a run
    of numerals and number words (no homophones), joined loosely (_LOOSE_JOINS), one after
    another, read exactly them. A longer number that holds them inside a token ("133445") does
    not, nor does a phone number that ends in them."""
    return any(
        _says_in_run(line, run, digits)
        for line in text.split("\n")
        for run in _runs(line, tokens=_SPOKEN_TOKEN, loose=True)
    )


def _allowed_spans(line: str) -> list[tuple[int, int]]:
    dates = [match.span() for match in _IDENTIFIER.finditer(line) if match.lastgroup in _DATE_KINDS]
    years = [match.span() for match in _YEAR_WORDS.finditer(line)]
    return sorted(dates + years)


def _kept(tokens: list[_Token], spans: list[tuple[int, int]]) -> list[bool]:
    """For each token, in order: inside an allowed span, or a year in numerals."""
    kept = []
    first = 0
    for token in tokens:
        while first < len(spans) and spans[first][1] <= token.start:
            first += 1
        inside = False
        index = first
        while index < len(spans) and spans[index][0] <= token.start:
            if token.end <= spans[index][1]:
                inside = True
                break
            index += 1
        year = token.word.isdigit() and len(token.word) == 4 and int(token.word) in _YEARS
        kept.append(inside or year)
    return kept


def _mask_unlisted(line: str) -> str:
    runs = _runs(line)
    flags = iter(_kept([token for run in runs for token in run], _allowed_spans(line)))
    spans = []
    for run in runs:
        kept = [next(flags) for _token in run]
        # The run, cut at its allowed tokens; a piece that reads as a callback number stays.
        pieces: list[list[_Token]] = [[]]
        for token, allowed in zip(run, kept, strict=True):
            if allowed:
                pieces.append([])
            else:
                pieces[-1].append(token)
        for piece in pieces:
            if len(_digits(piece)) in _CALLBACK_LENGTHS:
                continue
            # Each stretch of counted tokens, hidden as one.
            group: list[_Token] = []
            for token in piece:
                if token.counted:
                    group.append(token)
                    continue
                if group:
                    spans.append((group[0].start, group[-1].end, "****"))
                group = []
            if group:
                spans.append((group[0].start, group[-1].end, "****"))
    return _replaced(line, spans)


def mask_unverified_digits(text: str) -> str:
    """For an unverified caller: every digit and number word hidden, line by line, except
    inside a date `mask` recognises, a year from 1900 to 2099 in numerals or words, or a run
    that reads as ten or eleven digits (a callback number)."""
    return "\n".join(_mask_unlisted(line) for line in text.split("\n"))


def mask_known_digits(text: str, digits: str) -> str:
    """The text with every run that reads as exactly `digits` hidden, line by line. A longer
    run that holds them, such as a phone number, is left as it is."""
    lines = []
    for line in text.split("\n"):
        spans = []
        for run in _runs(line):
            for group in _groups(run):
                if _digits(group) == digits:
                    spans.append((group[0].start, group[-1].end, "****"))
                    break
        lines.append(_replaced(line, spans))
    return "\n".join(lines)


# A word of a name: letters, with an apostrophe inside (O'Brien). Shorter words are not hidden.
NAME_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")
MIN_NAME_LETTERS = 2
# What goes between the letters of a name spelled out: S A R A H, j-o-h-n-s-o-n, s.a.r.a.h.
SPELLED_GAP = r"[\s.,-]+"
PERSON = "<PERSON>"


def hide_names(text: str, names: Iterable[str]) -> str:
    """The text with each word of the names, of MIN_NAME_LETTERS or more, replaced by PERSON
    wherever it stands as a whole word, in any case, as it is or spelled out: one compiled
    alternation, so one pass over the text. Only names the caller knows are hidden; `mask`
    finds none by itself."""
    words = {
        word.lower()
        for name in names
        for word in NAME_WORD.findall(name)
        if len(word) >= MIN_NAME_LETTERS
    }
    if not words:
        return text
    forms = {re.escape(word) for word in words} | {
        SPELLED_GAP.join(re.escape(letter) for letter in word if letter != "'") for word in words
    }
    alternation = "|".join(sorted(forms, key=len, reverse=True))
    return re.sub(rf"(?<!\w)(?:{alternation})(?!\w)", PERSON, text, flags=re.IGNORECASE)
