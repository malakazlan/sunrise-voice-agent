import json
import random
import subprocess
import sys

from conftest import REPO_ROOT

from app.guard.mask import mask, mask_ssns

# Identifier samples are assembled from short pieces, so that no tracked file holds one.
SSN_PARTS = ("123", "45", "6789")
SSN = "-".join(SSN_PARTS)
SSN_PLAIN = "".join(SSN_PARTS)
LAST_FOUR = "33" + "44"
PHONE_PARTS = ("555", "201", LAST_FOUR)
PHONE = "-".join(PHONE_PARTS)

TABLE = [
    # SSN, grouped
    (SSN, "***-**-****"),
    (" ".join(SSN_PARTS), "***-**-****"),
    (".".join(SSN_PARTS), "***-**-****"),
    # SSN, nine digits alone
    (SSN_PLAIN, "***-**-****"),
    # Digits after an SSN cue
    (f"last four {LAST_FOUR}", "last four ****"),
    (f"last 4 {LAST_FOUR}", "last 4 ****"),
    (f"last-four {LAST_FOUR}", "last-four ****"),
    (f"ssn {LAST_FOUR}", "ssn ****"),
    (f"social {LAST_FOUR}", "social ****"),
    (f"ending in {LAST_FOUR}", "ending in ****"),
    (f"ends in {LAST_FOUR}", "ends in ****"),
    (f"ssn ({LAST_FOUR})", "ssn (****)"),
    (f"ssn [{LAST_FOUR}]", "ssn [****]"),
    (f"SSN{SSN}", "SSN****"),
    (f"SSN{SSN_PLAIN}", "SSN****"),
    ("social is three three four four", "social is ****"),
    ("last four thirty three forty four", "last four ****"),
    ("last four double three four four", "last four ****"),
    # A cue followed by another cue
    (
        f"my social security number last four is {LAST_FOUR}",
        "my social security number last four is ****",
    ),
    (f"ssn last 4 is {LAST_FOUR}", "ssn last 4 is ****"),
    (f"my social's last four are {LAST_FOUR}", "my social's last four are ****"),
    (f"the ssn last four digits are {LAST_FOUR}", "the ssn last four digits are ****"),
    (
        f"my social security number, the last four digits, are {LAST_FOUR}",
        "my social security number, the last four digits, are ****",
    ),
    # The repo's own field names
    (f"ssn_last4={LAST_FOUR}", "ssn_last4=****"),
    (f"ssn_last4='{LAST_FOUR}'", "ssn_last4='****'"),
    (f'"ssn_last4": "{LAST_FOUR}"', '"ssn_last4": "****"'),
    (f"last4: {LAST_FOUR}", "last4: ****"),
    (f"ssn_last_four={LAST_FOUR}", "ssn_last_four=****"),
    (f"last_four {LAST_FOUR}", "last_four ****"),
    # ISO date
    ("1985-03-14", "****-**-14"),
    ("1985-3-14", "****-**-14"),
    ("1985/03/14", "****-**-14"),
    ("dob1985-03-14", "dob****-**-14"),
    # Numeric date
    ("3/14/1985", "****-**-14"),
    ("03/14/85", "****-**-14"),
    ("3-14-1985", "****-**-14"),
    ("14.03.1985", "****-**-14"),
    # Spoken date
    ("March 14th 1985", "****-**-14"),
    ("March 14, 1985", "****-**-14"),
    ("March 14,1985", "****-**-14"),
    ("14 March 1985", "****-**-14"),
    ("Mar 14 1985", "****-**-14"),
    ("14 Mar 85", "****-**-14"),
    ("14-Mar-1985", "****-**-14"),
    ("fourteenth of March 1985", "****-**-14"),
    ("March fourteenth 1985", "****-**-14"),
    # Python's date repr
    ("datetime.date(1985, 3, 14)", "****-**-14"),
    ("datetime.datetime(1985, 3, 14, 10, 30)", "****-**-14, 10, 30)"),
    # Phone
    (PHONE, "***-***-3344"),
    (f"(555) 201-{LAST_FOUR}", "***-***-3344"),
    (".".join(PHONE_PARTS), "***-***-3344"),
    ("".join(PHONE_PARTS), "***-***-3344"),
    ("+1 " + " ".join(PHONE_PARTS), "***-***-3344"),
    (f"555/201-{LAST_FOUR}", "***-***-3344"),
    ("  ".join(PHONE_PARTS), "***-***-3344"),
    ("five five five two zero one three three four four", "***-***-3344"),
    # Email
    ("sarah.j@example.com", "s***@***.com"),
]

# The same kinds inside ordinary text: the text around them is kept as it is.
IN_TEXT = [
    (f"my number is {PHONE}, thanks", "my number is ***-***-3344, thanks"),
    ("born March 14th 1985 in Ohio", "born ****-**-14 in Ohio"),
    (f"social security number is {SSN_PLAIN}", "social security number is ****"),
    (f"Social is {LAST_FOUR}.", "Social is ****."),
    (f"the last four of my social are {LAST_FOUR}", "the last four of my social are ****"),
    (f"ssn: {SSN}", "ssn: ****"),
    ("seen 1985-03-14T10:30:00", "seen ****-**-14T10:30:00"),
    ("born 3/4/1985", "born ****-**-04"),
    ("write to sarah.j@example.com.", "write to s***@***.com."),
    (
        f"first 1985-03-14\nsecond {'.'.join(PHONE_PARTS)}\nthird sarah.j@example.com",
        "first ****-**-14\nsecond ***-***-3344\nthird s***@***.com",
    ),
    # The repr of a model holding a date of birth, the last four and a phone number.
    (
        f"Patient(id='P001', dob=datetime.date(1985, 3, 14), ssn_last4='{LAST_FOUR}',"
        f" phone='{PHONE}')",
        "Patient(id='P001', dob=****-**-14, ssn_last4='****', phone='***-***-3344')",
    ),
]

UUID_DIGITS_ONLY = "-".join(["12345678", "1234", "5678", "9012", "345678901234"])
UUID_HEX = "-".join(["3f2a9c1e", "5b7d", "4e2a", "9c1f", "0a1b2c3d4e5f"])
UUID_COMPACT = "".join(["a1234567", "89b0", "1234", "5678", "c90123456789"])

LEFT_ALONE = [
    f"call_started call_id={UUID_DIGITS_ONLY} active=2",
    f"call_audio call_id={UUID_HEX} frames_echoed=1500",
    f"call {UUID_COMPACT}",
    "runtime_role_ready created=False tables=6 sequences=3 functions=2",
    "seed_finished patients=5 medications=7 appointments=3 task_slots=12 generated_slots=40",
    "Running upgrade 5ec4a5850975 -> 5eaa0e7deda7, enforce audit chain position",
    "row 42",
    "id=1234",
    "patient P001",
    "P001",
    "10:30 AM",
    "at 9:05",
    "14 March at 10:30 AM",
    "$45.00",
    "$1,250.00",
    "1985",
    "in 2026",
    LAST_FOUR,
    f"code {LAST_FOUR}",
    "1.17.0",
    "v0.54.0",
    "Python 3.12.4",
    "tzdata 2026.6",
    "datetime.time(10, 30)",
    "Please hold while I check the schedule.",
    "Your appointment is on Tuesday at 10:30 AM with Dr. Patel.",
    "First of all, fourteen people called in the last hour, one second apart.",
    "",
]

HOSTILE = [
    "",
    "\x00\x01\x02\x1b[31m\x7f\r\n\t",
    "\ud800",
    "\udfff alone \ud800",
    "Привет 你好 مرحبا שלום नमस्ते",
    "１２３-４５-６７８９",
    "١٢٣-٤٥-٦٧٨٩",
    "é́́ \U0001f600 ‮ reversed",
    "a" * 100_000,
    "\n" * 20_000,
    "1" * 50_000,
    SSN_PLAIN * 3_000,
    (SSN_PLAIN + " ") * 1_500,
    ("".join(PHONE_PARTS) + " ") * 1_000,
    "1/1/00 " * 2_000,
    "ssn 4 " * 2_000,
    "a@b.co " * 2_000,
    "sarah.j@example.com " * 1_000,
    "@" * 20_000,
    f"x@y.com{'1985-03-14'} {PHONE}-12-15 {SSN}(555) 201-{LAST_FOUR}",
    "ssn " + "".join(PHONE_PARTS) + "@example.com x@y.mar 14 1985 x@y.com@z.org",
    # Number words that end just before a date the masking replaces.
    "five five five two zero one three three four four" + "1985-03-14",
    # A line that grows past 10,000 characters when masked, with a run of digits at the cut.
    (SSN_PLAIN + " ") * 832 + "".join(["1234567890", "123"]) + " " + "x" * 10,
]


def masked_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    return [(given, mask(given), expected) for given, expected in rows if mask(given) != expected]


def test_mask_table() -> None:
    assert masked_rows(TABLE) == []
    assert masked_rows(IN_TEXT) == []


def test_mask_leaves_other_text_alone() -> None:
    assert [text for text in LEFT_ALONE if mask(text) != text] == []


def test_mask_ssns_masks_only_ssns() -> None:
    rows = [
        (SSN, "***-**-****"),
        (SSN_PLAIN, "***-**-****"),
        (f"call about {SSN} today", "call about ***-**-**** today"),
        (f"my ssn last four is {LAST_FOUR}", "my ssn last four is ****"),
        (f"social {LAST_FOUR}\nnext line {SSN}", "social ****\nnext line ***-**-****"),
        # Phone numbers, dates and email addresses are left as they are.
        (f"call {PHONE} after 1985-03-14", f"call {PHONE} after 1985-03-14"),
        ("write to sarah.j@example.com", "write to sarah.j@example.com"),
        (f"{PHONE} or {SSN}", f"{PHONE} or ***-**-****"),
    ]
    assert [
        (given, mask_ssns(given)) for given, expected in rows if mask_ssns(given) != expected
    ] == []
    assert [text for text in LEFT_ALONE if mask_ssns(text) != text] == []
    assert [
        text[:80] for text in every_sample() if mask_ssns(mask_ssns(text)) != mask_ssns(text)
    ] == []


def fuzzed(count: int) -> list[str]:
    generator = random.Random(6)  # noqa: S311 (repeatable test input, not a secret)
    pieces = [
        *"0123456789 -./()+@:,'\n\t*",
        "ssn ",
        "last four ",
        "social ",
        "March ",
        "fourteenth ",
        "three ",
        "datetime.date(",
        ", ",
        "x@y.com",
        "\x00",
        "\ud800",
        "é",
        "你",
    ]
    return ["".join(generator.choices(pieces, k=generator.randint(0, 120))) for _ in range(count)]


def every_sample() -> list[str]:
    rows = [given for given, _expected in TABLE + IN_TEXT]
    return rows + LEFT_ALONE + HOSTILE + fuzzed(2_000)


def test_mask_is_idempotent() -> None:
    assert [text[:80] for text in every_sample() if mask(mask(text)) != mask(text)] == []


def test_mask_never_raises() -> None:
    for text in HOSTILE + fuzzed(500):
        assert isinstance(mask(text), str)

    # A line longer than 10,000 characters is cut to that length, a marker included, before
    # matching; every line on its own.
    assert mask("a" * 20_000) == "a" * 9_994 + " [cut]"
    assert mask("a" * 10_000) == "a" * 10_000
    assert mask("a" * 20_000 + "\nb") == "a" * 9_994 + " [cut]\nb"
    assert max(len(line) for text in HOSTILE for line in mask(text).split("\n")) <= 10_000


def test_long_traceback_keeps_its_last_line() -> None:
    frames = '  File "/app/app/db/session.py", line 120, in connect\n    return engine.connect()\n'
    traceback = "Traceback (most recent call last):\n" + frames * 250
    text = traceback + f"OperationalError: no route to the database, caller {PHONE}"
    assert len(text) > 20_000

    assert mask(text).split("\n")[-1] == (
        "OperationalError: no route to the database, caller ***-***-3344"
    )


# Input a backtracking pattern takes seconds or longer over: long runs that almost match.
ADVERSARIAL = [
    "1" * 10_000,
    "1-" * 5_000,
    "1 " * 5_000,
    "12/" * 3_333,
    "12-34-" * 1_666,
    "123-45-" * 1_428,
    "1985" + "-03-14" * 1_666,
    "1985/03/" * 1_250,
    "14.03." * 1_666,
    "14" + " Mar 1985" * 1_110,
    "Mar 14" + " 1985 Mar 14" * 832,
    "14 of March " * 833,
    "March fourteenth " * 588,
    "fourteenth of " * 714,
    "a" * 10_000,
    "a." * 5_000,
    "a@" * 5_000,
    "a@" + "b." * 4_999,
    "a@" + "b" * 5_000 + "_",
    "a@" + "b-" * 4_999,
    "a.b@" * 2_500,
    "a" * 9_990 + "@example.c",
    "social " * 1_428,
    "social is " * 1_000,
    "last four " * 1_000,
    "last four of my " * 625,
    "social security number " * 434,
    "ssn_last4=" * 1_000,
    "ssn " + "1 " * 4_998,
    "ssn one two three " * 555,
    "four " * 2_000,
    "thirty " * 1_428,
    "double " * 1_428,
    "five five five two zero one three three four " * 222,
    "(555) " * 1_666,
    "(555) 201-" * 1_000,
    "+1 " * 3_333,
    "+1 555 201 " * 909,
    "555-201-" * 1_250,
    "datetime.date(1985, 3, " * 434,
    " " * 10_000,
    "-" * 10_000,
    "." * 10_000,
    "/" * 10_000,
    "," * 10_000,
    "\t" * 10_000,
    "'" * 10_000,
    "(" * 10_000,
    "[" * 10_000,
    "*-" * 5_000,
]

# Seconds for one call on 10,000 characters. Measured well under this (see the report); a
# pattern that backtracks over the whole run takes far longer.
LINEAR_BOUND_SECONDS = 0.25
# The measuring process is stopped after this, so that a pattern that never finishes fails the
# test instead of hanging it.
MEASURE_TIMEOUT_SECONDS = 120

MEASURE = """
import json, sys, time
from app.guard.mask import mask
timings = []
for text in json.load(sys.stdin):
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        mask(text)
        best = min(best, time.perf_counter() - started)
    timings.append(best)
print(json.dumps(timings))
"""


def test_mask_is_linear() -> None:
    assert max(len(text) for text in ADVERSARIAL) <= 10_000

    try:
        measured = subprocess.run(
            [sys.executable, "-c", MEASURE],
            input=json.dumps(ADVERSARIAL),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=MEASURE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(f"mask did not finish within {MEASURE_TIMEOUT_SECONDS} s") from None
    timings = json.loads(measured.stdout)

    slow = {
        text[:16]: f"{seconds * 1000:.1f} ms"
        for text, seconds in zip(ADVERSARIAL, timings, strict=True)
    }
    assert max(timings) < LINEAR_BOUND_SECONDS, slow
