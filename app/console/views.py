"""How the console words and lays out what it reads: labels, clinic times, lengths, transcript
lines, and where the masked values are. Nothing here writes markup: the templates' autoescape
escapes every value from the database."""

import re
from dataclasses import dataclass
from datetime import datetime

from app.domain.clinic import CLINIC_TZ

# The turns the agent withholds from the model, as app/agent/turn.py writes them in the record.
WITHHELD_TURNS = frozenset({"(turn withheld)", "(emergency turn withheld)"})
# What masking leaves in place of a value (app/guard/mask.py and app/agent/call_record.py):
# shown in a chip, so a reader sees at a glance that it ran.
MASKED_VALUE = re.compile(
    r"<(?:PERSON|LOCATION|DATE_TIME)>"
    r"|\*{3}-\*{3}-(?:\d{4}|\*{4})"
    r"|\*{4}-\*{2}-\d{2}"
    r"|\*{3}-\*{2}-\*{4}"
    r"|[^\s*]\*{3}@\*{3}\.[A-Za-z]+"
    r"|\*{4}"
)
ZONE = "US Eastern (America/New_York)"


@dataclass(frozen=True, slots=True)
class Label:
    """A value as the console names it: its words, its icon, and its tone (ok, emergency,
    danger or plain). The words always say it; colour never says it alone."""

    words: str
    icon: str
    tone: str = "plain"


END_REASONS = {
    "completed": Label("Completed", "check", "ok"),
    "emergency": Label("Emergency", "alert", "emergency"),
    "transferred": Label("Transferred", "transfer"),
    "caller_hung_up": Label("Hung up", "minus"),
    "idle_timeout": Label("Idle", "clock"),
    "max_duration": Label("Maximum length", "clock"),
    "error": Label("Error", "x", "danger"),
}
REQUEST_TYPES = {
    "refill": Label("Refill", "pill"),
    "booking": Label("Booking", "calendar"),
    "callback": Label("Callback", "phone"),
    "doctor_message": Label("Doctor message", "message"),
    "transfer": Label("Transfer", "transfer"),
}
# The filters' words for each type, in the order the chips show them.
REQUEST_FILTERS = {
    "refill": "Refills",
    "booking": "Bookings",
    "callback": "Callbacks",
    "doctor_message": "Doctor messages",
    "transfer": "Transfers",
}
# The icon of an event in "What happened", by its action; anything else is a dot.
EVENT_ICONS = {
    "call_started": "phone",
    "call_ended": "check",
    "call_closed": "check",
    "identity_claimed": "user",
    "identity_verified": "check",
    "identity_refused": "x",
    "identity_locked": "lock",
    "appointment_read": "calendar",
    "appointment_booked": "calendar",
    "refill_requested": "pill",
    "message_taken": "message",
    "doctor_message_sent": "message",
    "transfer_requested": "transfer",
    "emergency_detected": "alert",
    "guard_block": "shield",
}


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a transcript: who said it, what, the caller's turn number, and whether the
    turn was withheld."""

    who: str
    said: str
    turn: int | None
    withheld: bool


def transcript_lines(transcript: str) -> list[Line]:
    lines = []
    turn = 0
    for raw in transcript.split("\n"):
        who, colon, said = raw.partition(": ")
        if colon and who == "caller":
            turn += 1
            lines.append(Line("caller", said, turn, said in WITHHELD_TURNS))
        elif colon and who == "agent":
            lines.append(Line("agent", said, None, False))
        elif raw.strip():
            lines.append(Line("", raw, None, False))
    return lines


@dataclass(frozen=True, slots=True)
class Piece:
    """A stretch of text, and whether it is what masking left in place of a value."""

    value: str
    masked: bool


def pieces(value: str) -> list[Piece]:
    """The text cut into its masked values and what lies between them. The template escapes
    every piece and puts each masked one in a <mark>."""
    found_pieces = []
    kept = 0
    for found in MASKED_VALUE.finditer(value):
        if found.start() > kept:
            found_pieces.append(Piece(value[kept : found.start()], False))
        found_pieces.append(Piece(found[0], True))
        kept = found.end()
    if kept < len(value):
        found_pieces.append(Piece(value[kept:], False))
    return found_pieces


def _clinic(instant: datetime) -> datetime:
    return instant.astimezone(CLINIC_TZ)


def _hour(local: datetime) -> str:
    return f"{local.hour % 12 or 12}:{local.minute:02d}"


def when(instant: datetime) -> str:
    """Nov 25, 10:27 AM, in clinic time."""
    local = _clinic(instant)
    return f"{local:%b} {local.day}, {_hour(local)} {local:%p}"


def clock(instant: datetime) -> str:
    """10:12:03 AM, in clinic time."""
    local = _clinic(instant)
    return f"{_hour(local)}:{local.second:02d} {local:%p}"


def day(instant: datetime) -> str:
    """Wednesday, November 25, 2026, in clinic time."""
    local = _clinic(instant)
    return f"{local:%A}, {local:%B} {local.day}, {local.year}"


def length(started: datetime, ended: datetime) -> str:
    """2 min 41 s."""
    seconds = max(int((ended - started).total_seconds()), 0)
    return f"{seconds // 60} min {seconds % 60:02d} s"


def short_id(call_id: str) -> str:
    """The first eight characters of a call id, as the console lists them."""
    return f"{call_id[:8]}…"
