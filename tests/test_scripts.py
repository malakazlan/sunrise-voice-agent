from app.agent.scripts import SCRIPTS, ScriptId

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
    (
        "fallback",
        "I'm sorry, I can't help with that. I can take a message for our staff instead.",
        True,
    ),
]


def test_lines_are_exact() -> None:
    assert [script.value for script in ScriptId] == [line[0] for line in LINES]
    assert [
        (script.value, SCRIPTS[script].text, SCRIPTS[script].interruptible) for script in SCRIPTS
    ] == LINES
    # The table cannot be changed while the application runs.
    assert type(SCRIPTS).__name__ == "mappingproxy"
