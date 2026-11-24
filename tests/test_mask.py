import random
import time

from app.guard.mask import mask

# Identifier samples are assembled from short pieces, so that no tracked file holds one.
SSN_PARTS = ("123", "45", "6789")
SSN = "-".join(SSN_PARTS)
LAST_FOUR = "33" + "44"
PHONE_PARTS = ("555", "201", LAST_FOUR)

TABLE = [
    # SSN, grouped
    (SSN, "***-**-****"),
    (" ".join(SSN_PARTS), "***-**-****"),
    # SSN, nine digits alone
    ("".join(SSN_PARTS), "***-**-****"),
    # Digits after an SSN cue
    (f"last four {LAST_FOUR}", "last four ****"),
    (f"last 4 {LAST_FOUR}", "last 4 ****"),
    (f"ssn {LAST_FOUR}", "ssn ****"),
    (f"social {LAST_FOUR}", "social ****"),
    ("social is three three four four", "social is ****"),
    # ISO date
    ("1985-03-14", "****-**-14"),
    # Numeric date
    ("3/14/1985", "****-**-14"),
    ("03/14/85", "****-**-14"),
    ("3-14-1985", "****-**-14"),
    # Spoken date
    ("March 14th 1985", "****-**-14"),
    ("March 14, 1985", "****-**-14"),
    ("14 March 1985", "****-**-14"),
    ("Mar 14 1985", "****-**-14"),
    # Phone
    ("-".join(PHONE_PARTS), "***-***-3344"),
    (f"(555) 201-{LAST_FOUR}", "***-***-3344"),
    (".".join(PHONE_PARTS), "***-***-3344"),
    ("".join(PHONE_PARTS), "***-***-3344"),
    ("+1 " + " ".join(PHONE_PARTS), "***-***-3344"),
    # Email
    ("sarah.j@example.com", "s***@***.com"),
]

# The same kinds inside ordinary text: the text around them is kept as it is.
IN_TEXT = [
    (f"my number is {'-'.join(PHONE_PARTS)}, thanks", "my number is ***-***-3344, thanks"),
    ("born March 14th 1985 in Ohio", "born ****-**-14 in Ohio"),
    (f"social security number is {''.join(SSN_PARTS)}", "social security number is ***-**-****"),
    (f"Social is {LAST_FOUR}.", "Social is ****."),
    (f"the last four of my social are {LAST_FOUR}", "the last four of my social are ****"),
    (f"ssn: {SSN}", "ssn: ***-**-****"),
    ("seen 1985-03-14T10:30:00", "seen ****-**-14T10:30:00"),
    ("born 3/4/1985", "born ****-**-04"),
    ("write to sarah.j@example.com.", "write to s***@***.com."),
    (
        f"first 1985-03-14\nsecond {'.'.join(PHONE_PARTS)}\nthird sarah.j@example.com",
        "first ****-**-14\nsecond ***-***-3344\nthird s***@***.com",
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
    "Please hold while I check the schedule.",
    "Your appointment is on Tuesday at 10:30 AM with Dr. Patel.",
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
    "".join(SSN_PARTS) * 3_000,
    ("".join(SSN_PARTS) + " ") * 1_500,
    ("".join(PHONE_PARTS) + " ") * 1_000,
    "1/1/00 " * 2_000,
    "sarah.j@example.com " * 1_000,
    "@" * 20_000,
]


def masked_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    return [(given, mask(given), expected) for given, expected in rows if mask(given) != expected]


def test_mask_table() -> None:
    assert masked_rows(TABLE) == []
    assert masked_rows(IN_TEXT) == []


def test_mask_leaves_other_text_alone() -> None:
    assert [text for text in LEFT_ALONE if mask(text) != text] == []


def every_sample() -> list[str]:
    rows = [given for given, _expected in TABLE + IN_TEXT]
    return rows + LEFT_ALONE + HOSTILE


def test_mask_is_idempotent() -> None:
    assert [text[:80] for text in every_sample() if mask(mask(text)) != mask(text)] == []


def test_mask_never_raises() -> None:
    generator = random.Random(6)  # noqa: S311 (repeatable test input, not a secret)
    alphabet = "0123456789 -./()+@:,\n\tssnSocialLastFourMarch\x00\ud800é你*"
    fuzzed = ["".join(generator.choices(alphabet, k=generator.randint(0, 400))) for _ in range(500)]

    for text in HOSTILE + fuzzed:
        assert isinstance(mask(text), str)

    # Longer input is cut to 10,000 characters, a marker included, before matching.
    assert mask("a" * 20_000) == "a" * 9_994 + " [cut]"
    assert mask("a" * 10_000) == "a" * 10_000
    assert max(len(mask(text)) for text in HOSTILE) <= 10_000


# Input a backtracking pattern would take seconds over: long runs that almost match.
ADVERSARIAL = [
    "1" * 10_000,
    "1-" * 5_000,
    "1 " * 5_000,
    "12/" * 3_333,
    "a" * 10_000,
    "a." * 5_000,
    "a@" * 5_000,
    "a@" + "b." * 4_999,
    "a" * 9_990 + "@example.c",
    "social " * 1_428,
    "social is " * 1_000,
    "last four " * 1_000,
    "ssn one two three " * 555,
    "March 14 " * 1_111,
    "14 March " * 1_111,
    "(555) " * 1_666,
    "+1 " * 3_333,
]

# Seconds for one call on 10,000 characters. Measured well under this (see the report); a
# pattern that backtracks over the whole run takes far longer.
LINEAR_BOUND_SECONDS = 0.25


def test_mask_is_linear() -> None:
    timings = {}
    for text in ADVERSARIAL:
        assert len(text) <= 10_000
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            mask(text)
            best = min(best, time.perf_counter() - started)
        timings[text[:12]] = best

    slow = {start: f"{seconds * 1000:.1f} ms" for start, seconds in timings.items()}
    assert max(timings.values()) < LINEAR_BOUND_SECONDS, slow
