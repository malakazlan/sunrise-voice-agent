"""The fixed lines code speaks, each with an id and whether the caller may interrupt it.

A tool outcome or a guard decision names a line by its id; the text is never the model's.
`verified` carries the caller's first name in `{first_name}`.
"""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class ScriptId(StrEnum):
    GREETING = "greeting"
    CLOSED = "closed"
    SSN_ASK = "ssn_ask"
    SSN_TOO_LONG = "ssn_too_long"
    VERIFIED = "verified"
    VERIFY_FAIL = "verify_fail"
    VERIFY_LOCKED = "verify_locked"
    EMERGENCY = "emergency"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class Script:
    text: str
    interruptible: bool


SCRIPTS = MappingProxyType(
    {
        ScriptId.GREETING: Script(
            "Thank you for calling Sunrise Family Clinic. This call may be recorded for quality"
            " purposes. How can I help you today?",
            interruptible=False,
        ),
        ScriptId.CLOSED: Script(
            "Our office is currently closed. Our hours are Monday to Friday, 9 AM to 5 PM"
            " Eastern Time. If this is a medical emergency, please hang up and call 911. I can"
            " take a message for our team.",
            interruptible=False,
        ),
        ScriptId.SSN_ASK: Script(
            "Thank you. Now please say only the last four digits of your Social Security number.",
            interruptible=True,
        ),
        ScriptId.SSN_TOO_LONG: Script(
            "For your security, please say only the last four digits.", interruptible=True
        ),
        ScriptId.VERIFIED: Script(
            "Thank you, {first_name}. You're verified. How can I help you today?",
            interruptible=True,
        ),
        ScriptId.VERIFY_FAIL: Script(
            "I'm sorry, I couldn't verify those details. Let's try again. Please say your full"
            " name and spell your last name, then your date of birth. If this is your first"
            " visit, I can help you book as a new patient instead.",
            interruptible=True,
        ),
        ScriptId.VERIFY_LOCKED: Script(
            "I'm sorry, I'm not able to verify your identity on this call. I can transfer you"
            " to our staff.",
            interruptible=True,
        ),
        ScriptId.EMERGENCY: Script(
            "If you're experiencing a medical emergency, please hang up and call 911 or go to"
            " the nearest emergency room right away.",
            interruptible=False,
        ),
        # For a model turn that failed or ran too long (app/agent/turn.py).
        ScriptId.FALLBACK: Script(
            "I'm sorry, I can't help with that. I can take a message for our staff instead.",
            interruptible=True,
        ),
    }
)
