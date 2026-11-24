from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.agent.scripts import SCRIPTS, ScriptId
from app.domain.office_hours import part_of_day

# Every scripted line, written by hand from the spec: id, text, whether it may be interrupted.
LINES = [
    (
        "greeting",
        "Thank you for calling Sunrise Family Clinic. This call may be recorded for quality"
        " purposes. How can I help you today?",
        False,
    ),
    (
        "closed",
        "Our office is currently closed. Our hours are Monday to Friday, 9 AM to 5 PM Eastern"
        " Time. If this is a medical emergency, please hang up and call 911. I can take a message"
        " for our team.",
        False,
    ),
    (
        "ssn_ask",
        "Thank you. Now please say only the last four digits of your Social Security number.",
        True,
    ),
    ("ssn_too_long", "For your security, please say only the last four digits.", True),
    ("verified", "Thank you, {first_name}. You're verified. How can I help you today?", True),
    (
        "verify_fail",
        "I'm sorry, I couldn't verify those details. Let's try again. Please say your full name"
        " and spell your last name, then your date of birth. If this is your first visit, I can"
        " help you book as a new patient instead.",
        True,
    ),
    (
        "verify_locked",
        "I'm sorry, I'm not able to verify your identity on this call. I can transfer you to our"
        " staff.",
        True,
    ),
    (
        "emergency",
        "If you're experiencing a medical emergency, please hang up and call 911 or go to the"
        " nearest emergency room right away.",
        False,
    ),
    ("transfer", "I'm transferring you to our staff now. Please hold.", True),
    ("goodbye", "Thank you for calling Sunrise Family Clinic. Goodbye.", True),
    (
        "after_hours_close",
        "Thank you, {name}. Your message has been recorded, and our team will get back to you on"
        " the next business day. If this is an emergency, please hang up and call 911. Have a"
        " good {part_of_day}.",
        True,
    ),
    (
        "guard_fallback",
        "I'm sorry, I can't help with that. I can take a message for our staff instead.",
        True,
    ),
]

# The part of the day the after-hours line wishes the caller, written by hand: `day` before
# 5 PM clinic time, `night` from then on, in winter (UTC-5) and summer (UTC-4) alike.
PARTS_OF_DAY = [
    # Tuesday 24 November 2026, in clinic time.
    (datetime(2026, 11, 24, 21, 59, 59, 999999, tzinfo=UTC), "day"),
    (datetime(2026, 11, 24, 22, 0, tzinfo=UTC), "night"),
    (datetime(2026, 11, 25, 4, 30, tzinfo=UTC), "night"),
    # 02:00 and 07:00 on Wednesday: before 5 PM.
    (datetime(2026, 11, 25, 7, 0, tzinfo=UTC), "day"),
    (datetime(2026, 11, 25, 12, 0, tzinfo=UTC), "day"),
    # Saturday 28 November, 10:00 and 18:00.
    (datetime(2026, 11, 28, 15, 0, tzinfo=UTC), "day"),
    (datetime(2026, 11, 28, 23, 0, tzinfo=UTC), "night"),
    # Saturday 4 July: 16:59 and 17:00 in clinic time are 20:59 and 21:00 UTC.
    (datetime(2026, 7, 4, 20, 59, tzinfo=UTC), "day"),
    (datetime(2026, 7, 4, 21, 0, tzinfo=UTC), "night"),
    # 14:30 in a zone eight hours behind UTC is 17:30 in clinic time.
    (datetime(2026, 11, 24, 14, 30, tzinfo=timezone(timedelta(hours=-8))), "night"),
]


def test_lines_are_exact() -> None:
    assert [script.value for script in ScriptId] == [line[0] for line in LINES]
    assert [
        (script.value, SCRIPTS[script].text, SCRIPTS[script].interruptible) for script in SCRIPTS
    ] == LINES
    # The table cannot be changed while the application runs.
    assert type(SCRIPTS).__name__ == "mappingproxy"


def test_after_hours_line_follows_clinic_time() -> None:
    assert [(at, part_of_day(at)) for at, _ in PARTS_OF_DAY] == PARTS_OF_DAY
    with pytest.raises(ValueError, match="timezone"):
        part_of_day(datetime(2026, 11, 24, 18, 0))

    # The line as code speaks it, with its two values filled in.
    line = SCRIPTS[ScriptId.AFTER_HOURS_CLOSE].text.format(
        name="Ana Diaz", part_of_day=part_of_day(datetime(2026, 11, 25, 1, 0, tzinfo=UTC))
    )
    assert line == (
        "Thank you, Ana Diaz. Your message has been recorded, and our team will get back to you"
        " on the next business day. If this is an emergency, please hang up and call 911. Have a"
        " good night."
    )
