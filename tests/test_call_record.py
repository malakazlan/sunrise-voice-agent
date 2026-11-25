"""The record of an ended call: one row of call_sessions, its transcript masked by `mask` and then
by Presidio, on a worker thread of its own, after the call has ended. Presidio runs for real
here, with the spaCy model installed from the lock; nothing reaches the network. Expected text
is written by hand from the scripts, the seed data and the masking rules."""

import ast
import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import OFFLINE_DB_ENV, REPO_ROOT, offline_settings
from pipecat.frames.frames import InterruptionFrame, VADUserStartedSpeakingFrame
from speech_fakes import CallPipeline, FakeTime, FakeVoice, agent_call, caller_says, until
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from test_admin_api import admin_settings
from test_turn import (
    DIGITS_IN_WORDS,
    EMERGENCY,
    FIRST_PAIR,
    GREETING,
    SARAH_CLAIM,
    SECOND_PAIR,
    SEED_DAY,
    SSN_ASK,
    T0,
    agent_for,
    deps_on,
    everything_sent,
    new_call_id,
)

import app.agent.turn
from app.agent import call_record
from app.agent.call_record import drain_call_records, record_call
from app.agent.endings import END_PHASES, drain_call_events, end_call
from app.agent.model import ModelReply
from app.agent.state import CallState, Phase, move, new_call
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.audit.log import Reason
from app.db.models import CALL_END_REASONS, MAX_TRANSCRIPT
from app.db.seed import load_seed_data, seed
from app.db.session import create_database_engine
from app.guard.input import GuardAction
from app.guard.mask import hide_names, mask
from app.main import create_app
from evals.scripted import ScriptedModel, calls, says

pytestmark = pytest.mark.anyio

TURN_MODULE = REPO_ROOT / "app" / "agent" / "turn.py"

STORED = text(
    "SELECT call_id, started_at, ended_at, office_open, verified, patient_id, end_reason,"
    " turn_count, transcript, transcript_truncated, names_masked FROM call_sessions ORDER BY id"
)

# The caller's words, with every kind of identifier the two passes hide. Built from short
# pieces, like every identifier in the tests.
CLAIM = "Hi, this is Sarah Johnson, born March 14, 1985."
PHONE = "-".join(["555", "201", "7788"])
EMAIL = "sarah.j" + "@example.com"
CONTACT = f"Please call me back at {PHONE} or write to {EMAIL}."
CONTACT_REPLY = "I'll pass that on to the staff."
SSN_DIGITS = FIRST_PAIR + SECOND_PAIR
# The fixed line a swallowed turn leaves in the record, written out by hand.
SWALLOWED_LINE = "caller: (turn withheld)"
EMERGENCY_LINE = "caller: (emergency turn withheld)"
# When the call in the failure test ends: five minutes after it started.
LATER = T0 + timedelta(minutes=5)


def stored(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(STORED)]


def ended_call(call_id: str | None = None) -> CallState:
    return move(
        new_call(call_id=call_id or new_call_id(), started_at=T0, office_open=True), Phase.ENDED
    )


@pytest.fixture
async def deps(runtime: AsyncEngine, owner_session: AsyncSession) -> Deps:
    await seed(owner_session, load_seed_data(), SEED_DAY)
    await owner_session.commit()
    return deps_on(runtime)


@pytest.fixture(scope="module")
def presidio() -> None:
    """The real engines, loaded once for the module: loading the model takes about a second."""
    call_record._engines()


@pytest.fixture
def plain_presidio(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Presidio pass leaves the text as it is: for tests about everything around it."""
    monkeypatch.setattr(call_record, "_scrub", lambda text: text)


def _is_record_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "record_call") or (
        isinstance(node, ast.Attribute) and node.attr == "record_call"
    )


def record_uses(source: str) -> list[str]:
    """Where a module names the call record: its imports, each function that calls record_call,
    and each function that awaits it."""
    uses = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and "call_record" in (node.module or ""):
            uses.append(f"from {node.module} import {', '.join(a.name for a in node.names)}")
        elif isinstance(node, ast.Import):
            uses += [f"import {a.name}" for a in node.names if "call_record" in a.name]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and _is_record_call(inner.func):
                    uses.append(f"call in {node.name}")
                elif isinstance(inner, ast.Await) and any(
                    _is_record_call(part) for part in ast.walk(inner)
                ):
                    uses.append(f"await in {node.name}")
    return uses


# --- R8 ---------------------------------------------------------------------------------


async def test_transcript_is_masked_and_late(
    deps: Deps, owner_engine: Engine, presidio: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    model = ScriptedModel(calls(("verify_identity", SARAH_CLAIM)), says(CONTACT_REPLY))
    agent = await agent_for(model, deps, register_tools())
    await agent.handle(CLAIM)
    assert (await agent.handle(DIGITS_IN_WORDS)).action is GuardAction.SWALLOW
    await agent.handle(CONTACT)
    # The model was given nothing of the swallowed turn, not even its marker.
    assert "withheld" not in everything_sent(model)
    assert DIGITS_IN_WORDS not in everything_sent(model)

    # Late: a call that has not ended is not recorded, nor an end that is not one.
    for state, reason in ((agent.state, Reason.CALLER_HUNG_UP), (agent.state, Reason.GUARD)):
        with pytest.raises(ValueError):
            record_call(state, agent.history, reason, deps)
    ended = await end_call(agent.state, Reason.CALLER_HUNG_UP, deps)
    with pytest.raises(ValueError):
        record_call(ended, agent.history, Reason.MODEL, deps)
    await drain_call_records()
    assert stored(owner_engine) == []
    # The table takes every reason a call ends for, and only those.
    assert set(CALL_END_REASONS) == {reason.value for reason in END_PHASES}

    # Recorded twice, stored once.
    record_call(ended, agent.history, Reason.CALLER_HUNG_UP, deps)
    record_call(ended, agent.history, Reason.CALLER_HUNG_UP, deps)
    await drain_call_records()
    await drain_call_events()

    [row] = stored(owner_engine)
    # Three caller turns: the claim and the contact turn the model was shown, and the digits
    # it was not.
    assert row[:8] == (ended.call_id, T0, T0, True, True, "P001", "caller_hung_up", 3)
    assert row[9:] == (False, True)
    transcript = row[8]
    for raw in (
        "Sarah",
        "Johnson",
        "March 14",
        "1985",
        PHONE,
        "201-7788",
        EMAIL,
        "sarah.j",
        "example",
        SSN_DIGITS,
        DIGITS_IN_WORDS,
        "three",
    ):
        assert raw not in transcript, raw
    # mask's work and Presidio's both show.
    assert "***-***-7788" in transcript
    assert "s***@***.com" in transcript
    assert transcript.count("<PERSON>") >= 2
    assert "<DATE_TIME>" in transcript
    lines = transcript.split("\n")
    assert [line.split(": ", 1)[0] for line in lines] == [
        "agent",
        "caller",
        "agent",
        "caller",
        "agent",
        "caller",
        "agent",
    ]
    # Presidio may also take an ordinary word ("today") for a date, so the lines that hold one
    # are compared up to it.
    assert lines[0].startswith(f"agent: {GREETING.split(' How')[0]} How can I help you ")
    assert lines[2] == f"agent: {SSN_ASK}"
    # The swallowed turn is where it was, as the fixed marker, before the line it led to.
    assert lines[3] == SWALLOWED_LINE
    assert transcript.count("withheld") == 1
    assert lines[4].startswith("agent: Thank you, <PERSON>. You're verified. How can I help you ")
    assert lines[6] == f"agent: {CONTACT_REPLY}"

    # The logs name the record, never its text.
    recorded = [r.getMessage() for r in caplog.records if r.name == "app.agent.call_record"]
    # The second record of the call stored nothing, and says so.
    assert sorted(recorded) == [
        f"call_record_duplicate call_id={ended.call_id}",
        f"call_recorded call_id={ended.call_id} turns=3 truncated=False",
    ]
    for raw in ("Sarah", "Johnson", PHONE, EMAIL, SSN_DIGITS, DIGITS_IN_WORDS, CONTACT_REPLY):
        assert raw not in caplog.text, raw

    # Never on the turn path: the turn module calls the record in `end` alone, and does not
    # await it.
    assert record_uses("async def _handle(s):\n    record_call(s)\n") == ["call in _handle"]
    assert record_uses("async def end(s):\n    await call_record.record_call(s)\n") == [
        "await in end",
        "call in end",
    ]
    assert record_uses("import app.agent.call_record\n") == ["import app.agent.call_record"]
    assert record_uses(TURN_MODULE.read_text(encoding="utf-8")) == [
        "from app.agent.call_record import record_call",
        "call in end",
    ]


async def test_each_swallowed_turn_leaves_one_marker(
    deps: Deps, owner_engine: Engine, plain_presidio: None
) -> None:
    model = ScriptedModel(calls(("verify_identity", SARAH_CLAIM)))
    agent = await agent_for(model, deps, register_tools())
    await agent.handle(CLAIM)
    # The last four in two turns, both swallowed; the second submits them.
    words = DIGITS_IN_WORDS.split()
    for half in (words[:2], words[2:]):
        assert (await agent.handle(" ".join(half))).action is GuardAction.SWALLOW
    sent = [request.messages for request in model.requests]

    ended = await end_call(agent.state, Reason.CALLER_HUNG_UP, deps)
    record_call(ended, agent.history, Reason.CALLER_HUNG_UP, deps)
    await drain_call_records()
    await drain_call_events()

    [(turns, transcript)] = [(row[7], row[8]) for row in stored(owner_engine)]
    lines = transcript.split("\n")
    assert turns == 3
    assert [line.split(": ", 1)[0] for line in lines] == ["agent", "caller", "agent"] + [
        "caller",
        "caller",
        "agent",
    ]
    assert lines[2:] == [
        f"agent: {SSN_ASK}",
        SWALLOWED_LINE,
        SWALLOWED_LINE,
        "agent: Thank you, <PERSON>. You're verified. How can I help you today?",
    ]
    # Reading the record changed nothing the model is given.
    assert [request.messages for request in model.requests] == sent
    assert "withheld" not in everything_sent(model)


def tool_exchange(tool: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """A tool call as the conversation holds it, with text the model wrote beside it, and its
    result."""
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me note that."},
                {"type": "tool_use", "id": "toolu_9", "name": tool, "input": arguments},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "{}"}],
        },
    ]


# Each tool a name is given to, what it was given, what the caller said around it, and the
# lines kept: in lower case, alone, and spelled with spaces, dots, commas or hyphens.
NAMES_GIVEN_TO_TOOLS = {
    "take_message": (
        {"name": "Maria Lopez", "callback_number": PHONE, "reason_short": "a refill question"},
        ("yes it's maria", "thanks, that's lopez, l-o-p-e-z"),
        ("caller: yes it's <PERSON>", "caller: thanks, that's <PERSON>, <PERSON>"),
    ),
    # A claim that matched no patient: the name is still the caller's.
    "verify_identity": (
        {"full_name": "Tom Baker", "dob": "1990-01-01"},
        ("this is tom baker", "it's T O M, b.a.k.e.r"),
        ("caller: this is <PERSON> <PERSON>", "caller: it's <PERSON>, <PERSON>"),
    ),
    "collect_new_patient": (
        {"full_name": "Ana Silva", "dob": "1990-01-01", "phone": PHONE, "email": EMAIL},
        ("ana silva", "A-N-A, s, i, l, v, a"),
        ("caller: <PERSON> <PERSON>", "caller: <PERSON>, <PERSON>"),
    ),
}


@pytest.mark.parametrize("tool", list(NAMES_GIVEN_TO_TOOLS))
async def test_names_given_to_tools_are_hidden(
    tool: str, database: Any, runtime: AsyncEngine, owner_engine: Engine, plain_presidio: None
) -> None:
    arguments, (before, after), (kept_before, kept_after) = NAMES_GIVEN_TO_TOOLS[tool]
    history = [
        {"role": "user", "content": before},
        *tool_exchange(tool, arguments),
        {"role": "assistant", "content": "Thank you."},
        {"role": "user", "content": after},
    ]
    record_call(ended_call(), history, Reason.COMPLETED, deps_on(runtime))
    await drain_call_records()

    [(turns, transcript, names_masked)] = [(r[7], r[8], r[10]) for r in stored(owner_engine)]
    assert (turns, names_masked) == (2, True)
    assert transcript == f"{kept_before}\nagent: Thank you.\n{kept_after}"


def test_the_name_arguments_are_the_tools() -> None:
    assert call_record.NAME_ARGUMENTS == {
        "verify_identity": "full_name",
        "collect_new_patient": "full_name",
        "take_message": "name",
    }
    tools = {tool.name: tool for tool in register_tools().tools}
    for tool, argument in call_record.NAME_ARGUMENTS.items():
        assert argument in tools[tool].arguments.model_fields, tool


def test_short_name_words_are_not_hidden() -> None:
    # A one-letter word of a name would hide every such letter in the text.
    assert hide_names("a j or a J. Smith", ["J Smith"]) == "a j or a J. <PERSON>"
    assert hide_names("Smithson and smith", ["J Smith"]) == "Smithson and <PERSON>"


DIGIT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
VERIFIED_EMILY = "Thank you, Emily. You're verified. How can I help you today?"
HEART_ATTACK = "I think I'm having a heart attack"


async def verified_emily(deps: Deps) -> tuple[CallState, tuple[dict[str, Any], ...]]:
    """Patient P005 of the seed data verified, through the claim and her last four, and the
    call ended."""
    emily = next(p for p in load_seed_data()["patients"] if p["id"] == "P005")
    claim = {"full_name": f"{emily['first_name']} {emily['last_name']}", "dob": emily["dob"]}
    model = ScriptedModel(calls(("verify_identity", claim)))
    agent = await agent_for(model, deps, register_tools())
    await agent.handle("hi, this is emily nguyen")
    last_four = " ".join(DIGIT_WORDS[int(digit)] for digit in emily["ssn_last4"])
    assert (await agent.handle(last_four)).lines == (VERIFIED_EMILY,)
    return await end_call(agent.state, Reason.CALLER_HUNG_UP, deps), agent.history


async def test_the_verified_patients_names_are_hidden(
    deps: Deps, owner_engine: Engine, presidio: None
) -> None:
    ended, history = await verified_emily(deps)
    record_call(ended, history, Reason.CALLER_HUNG_UP, deps)
    await drain_call_records()
    await drain_call_events()

    [transcript] = [row[8] for row in stored(owner_engine)]
    assert "emily" not in transcript.casefold()
    assert "nguyen" not in transcript.casefold()
    lines = transcript.split("\n")
    assert lines[1] == "caller: hi, this is <PERSON> <PERSON>"
    assert lines[-1].startswith("agent: Thank you, <PERSON>. You're verified. How can I help ")


# What a verified caller's record holds that no tool was given: her name from the patients
# row, said and spelled.
SAID_TO_EMILY = [
    {"role": "assistant", "content": VERIFIED_EMILY},
    {"role": "user", "content": "nguyen, n g u y e n"},
]


async def test_the_patients_row_names_the_caller(
    deps: Deps, owner_engine: Engine, plain_presidio: None
) -> None:
    ended, _history = await verified_emily(deps)
    record_call(ended, SAID_TO_EMILY, Reason.CALLER_HUNG_UP, deps)
    await drain_call_records()
    await drain_call_events()

    assert [row[8] for row in stored(owner_engine)] == [
        "agent: Thank you, <PERSON>. You're verified. How can I help you today?\n"
        "caller: <PERSON>, <PERSON>"
    ]


async def test_a_failed_patient_lookup_keeps_the_first_name(
    deps: Deps,
    owner_engine: Engine,
    plain_presidio: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(_session: AsyncSession, _patient_id: str) -> list[str]:
        raise RuntimeError("the database went away")

    ended, _history = await verified_emily(deps)
    monkeypatch.setattr(call_record, "_patient_names", broken)
    caplog.set_level(logging.INFO)
    record_call(ended, SAID_TO_EMILY, Reason.CALLER_HUNG_UP, deps)
    await drain_call_records()
    await drain_call_events()

    # The first name the state holds is hidden; the last name only the row knew is not.
    assert [row[8] for row in stored(owner_engine)] == [
        "agent: Thank you, <PERSON>. You're verified. How can I help you today?\n"
        "caller: nguyen, n g u y e n"
    ]
    assert [r.getMessage() for r in caplog.records if r.name == "app.agent.call_record"] == [
        f"call_record_patient_lookup_failed call_id={ended.call_id} error=RuntimeError",
        f"call_recorded call_id={ended.call_id} turns=1 truncated=False",
    ]
    assert "went away" not in caplog.text


async def test_an_emergency_turn_leaves_its_marker(
    deps: Deps, owner_engine: Engine, plain_presidio: None
) -> None:
    model = ScriptedModel()
    agent = await agent_for(model, deps, register_tools())
    assert (await agent.handle(HEART_ATTACK)).action is GuardAction.EMERGENCY
    ended = await end_call(agent.state, Reason.EMERGENCY, deps)
    record_call(ended, agent.history, Reason.EMERGENCY, deps)
    await drain_call_records()
    await drain_call_events()

    assert [(row[7], row[8]) for row in stored(owner_engine)] == [
        (1, f"agent: {GREETING}\n{EMERGENCY_LINE}\nagent: {EMERGENCY}")
    ]
    assert model.requests == []


async def test_masking_runs_off_the_event_loop(
    database: Any, runtime: AsyncEngine, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: dict[str, int] = {}

    def mask_on(text: str) -> str:
        threads["mask"] = threading.get_ident()
        return mask(text)

    def scrub_on(text: str) -> str:
        threads["scrub"] = threading.get_ident()
        return text

    monkeypatch.setattr(call_record, "mask", mask_on)
    monkeypatch.setattr(call_record, "_scrub", scrub_on)
    history = [{"role": "assistant", "content": "Hello."}, {"role": "user", "content": "Hi."}]

    record_call(ended_call(), history, Reason.COMPLETED, deps_on(runtime))
    await drain_call_records()

    loop_thread = threading.get_ident()
    assert set(threads) == {"mask", "scrub"}
    assert loop_thread not in threads.values()
    assert [row[8] for row in stored(owner_engine)] == ["agent: Hello.\ncaller: Hi."]


# --- R9 ---------------------------------------------------------------------------------


async def test_presidio_failure_is_survivable(
    database: Any,
    runtime: AsyncEngine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def broken(_text: str) -> str:
        raise RuntimeError("the model would not load")

    monkeypatch.setattr(call_record, "_scrub", broken)
    history: list[dict[str, Any]] = [
        {"role": "assistant", "content": "How can I help?"},
        {"role": "user", "content": f"I'm Jane Roe, born 1985-03-14, call {PHONE}."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "toolu_0", "name": "get_balance", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_0", "content": "{}"}],
        },
        {"role": "assistant", "content": "Your balance is\ntwenty dollars."},
    ]
    state = ended_call()

    record_call(state, history, Reason.ERROR, deps_on(runtime, LATER))
    await drain_call_records()

    # mask's pass alone: the date and the number are hidden, the name is not.
    assert stored(owner_engine) == [
        (
            state.call_id,
            T0,
            LATER,
            True,
            False,
            None,
            "error",
            1,
            # The text the model wrote beside its tool call was never said: it is not kept.
            "agent: How can I help?\n"
            "caller: I'm Jane Roe, born ****-**-14, call ***-***-7788.\n"
            "agent: Your balance is twenty dollars.",
            False,
            False,
        )
    ]
    assert [r.getMessage() for r in caplog.records if r.name == "app.agent.call_record"] == [
        f"call_record_presidio_failed call_id={state.call_id} error=RuntimeError",
        f"call_recorded call_id={state.call_id} turns=1 truncated=False",
    ]
    assert "would not load" not in caplog.text
    assert "Jane" not in caplog.text


# --- bounds and failures ----------------------------------------------------------------


async def test_long_transcript_is_cut_at_a_line(
    database: Any, runtime: AsyncEngine, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each line is "caller: " and 92 letters: 100 characters, 101 with its newline. 198 lines
    # are 19,997 characters; 199 would be 20,098.
    line, grows = "a" * 92, "x" * 92
    seen: list[int] = []

    def scrub(text: str) -> str:
        # Presidio's pass, made to lengthen each line of x by one character.
        seen.append(len(text))
        return text.replace(grows, "y" * 93)

    monkeypatch.setattr(call_record, "_scrub", scrub)
    fits = [{"role": "user", "content": line}] * 198
    longer = [{"role": "user", "content": line}] * 250
    lengthened = [{"role": "user", "content": grows}] * 198
    short, long, masked_long = ended_call(), ended_call(), ended_call()

    for state, history in ((short, fits), (long, longer), (masked_long, lengthened)):
        record_call(state, history, Reason.COMPLETED, deps_on(runtime))
    await drain_call_records()

    kept = "\n".join([f"caller: {line}"] * 198)
    assert len(kept) == 19_997 <= MAX_TRANSCRIPT == 20_000
    # Lines of "caller: " and 93 letters: 196 of them are 19,991 characters, 197 would be 20,093.
    kept_masked = "\n".join(["caller: " + "y" * 93] * 196)
    assert len(kept_masked) == 19_991
    assert {row[0]: (row[7], row[8], row[9]) for row in stored(owner_engine)} == {
        short.call_id: (198, kept, False),
        long.call_id: (250, kept, True),
        masked_long.call_id: (198, kept_masked, True),
    }
    # The masking passes never read more than the most that is kept.
    assert seen == [19_997] * 3


async def test_a_failed_write_is_logged_and_dropped(
    caplog: pytest.LogCaptureFixture, plain_presidio: None
) -> None:
    # A database that cannot be reached: the record is lost, the failure logged by its class.
    engine = create_database_engine(offline_settings())
    caplog.set_level(logging.INFO)
    state = ended_call()
    try:
        record_call(state, [{"role": "user", "content": "Hi."}], Reason.COMPLETED, deps_on(engine))
        await drain_call_records()
    finally:
        await engine.dispose()
    assert [r.getMessage() for r in caplog.records if r.name == "app.agent.call_record"] == [
        f"call_record_failed call_id={state.call_id} error=OperationalError"
    ]


def test_the_model_is_never_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    def download(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the model was downloaded")

    monkeypatch.setattr("spacy.cli.download", download)
    with pytest.raises(RuntimeError, match="not installed"):
        call_record._load("en_core_web_not_installed")


def test_presidio_loads_on_first_use(tmp_path: Path) -> None:
    # In a fresh interpreter: importing the app and building it import neither Presidio nor
    # spaCy and load no model; the first Presidio pass does. Presidio's import gives its
    # decision log an unmasked handler at INFO: once loaded, that handler is gone and Presidio's
    # loggers keep only warnings and errors, as configure_logging set them.
    script = (
        "import logging, sys\n"
        "from app.main import create_app\n"
        "from app.agent import call_record\n"
        "create_app()\n"
        "loaded = ('presidio_analyzer', 'presidio_anonymizer', 'spacy', 'en_core_web_sm')\n"
        "def show():\n"
        "    print([name in sys.modules for name in loaded],"
        " call_record._engines.cache_info().currsize)\n"
        "    for name in ('presidio-analyzer', 'presidio-anonymizer', 'decision_process'):\n"
        "        each = logging.getLogger(name)\n"
        "        print(name, each.level, [type(h).__name__ for h in each.handlers])\n"
        "show()\n"
        "print(call_record._scrub('Sarah Johnson lives in Boston.'))\n"
        "show()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, **OFFLINE_DB_ENV, "PYTHONPATH": str(REPO_ROOT), "APP_ENV": "dev"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "[False, False, False, False] 0",
        "presidio-analyzer 30 []",
        "presidio-anonymizer 30 []",
        "decision_process 30 []",
        "<PERSON> lives in <LOCATION>.",
        "[True, True, True, True] 1",
        "presidio-analyzer 30 ['NullHandler']",
        "presidio-anonymizer 30 ['NullHandler']",
        "decision_process 30 []",
    ]


@pytest.fixture
def slow_presidio(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(text: str) -> str:
        time.sleep(0.5)
        return text

    monkeypatch.setattr(call_record, "_scrub", slow)


async def test_shutdown_waits_for_call_records(
    database: Any, owner_engine: Engine, slow_presidio: None
) -> None:
    app = create_app(admin_settings(database))
    state = ended_call()
    async with app.router.lifespan_context(app):
        record_call(state, [{"role": "user", "content": "Hi."}], Reason.COMPLETED, app.state.deps)
        await asyncio.sleep(0)
    assert [row[0] for row in stored(owner_engine)] == [state.call_id]


# --- the live call ----------------------------------------------------------------------

GOODBYE = "Thank you for calling Sunrise Family Clinic. Goodbye."
# Each way a live call ends here: what the caller says (nothing: they hang up), what the model
# does with it, the reason the record gives and the transcript kept, written by hand.
LIVE_ENDINGS: dict[str, tuple[str | None, list[ModelReply], str, str]] = {
    "goodbye": (
        "No, that's all. Thanks.",
        [calls(("close_call", {}))],
        "completed",
        f"agent: {GREETING}\ncaller: No, that's all. Thanks.\nagent: {GOODBYE}",
    ),
    "hang_up": (None, [], "caller_hung_up", f"agent: {GREETING}"),
    "emergency": (
        HEART_ATTACK,
        [],
        "emergency",
        f"agent: {GREETING}\n{EMERGENCY_LINE}\nagent: {EMERGENCY}",
    ),
}


@pytest.mark.parametrize("ending", list(LIVE_ENDINGS))
async def test_the_live_call_is_recorded_once(
    ending: str, runtime: AsyncEngine, owner_engine: Engine, plain_presidio: None
) -> None:
    caller, steps, reason, transcript = LIVE_ENDINGS[ending]
    time = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(call_id, deps_on(runtime), ScriptedModel(*steps), time)
    voice = FakeVoice()

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: len(voice.spoken) == 1, "the opening line")
        await until(lambda: time.waiting == 2, "the timers")
        if caller is None:
            await processor.hang_up()
        else:
            await call.send(*caller_says(caller))
            await until(lambda: call.ended, "the call to end")
    # A second ending changes nothing: the call ended, and was recorded, once.
    await processor.hang_up()
    await drain_call_records()
    await drain_call_events()

    assert [(row[0], row[6], row[8]) for row in stored(owner_engine)] == [
        (call_id, reason, transcript)
    ]


REPLY = "We are open Monday to Friday, from nine to five."


async def test_an_interrupted_reply_is_recorded_as_heard(
    runtime: AsyncEngine, owner_engine: Engine, plain_presidio: None
) -> None:
    model = ScriptedModel(says(REPLY), says("Sure, what is it?"))
    time = FakeTime()
    processor, playback = agent_call(new_call_id(), deps_on(runtime), model, time)
    voice = FakeVoice(autoplay=False)

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the greeting")
        await voice.play_all()
        await call.send(*caller_says("When are you open?"))
        await until(lambda: voice.waiting == 1, "the reply")
        # Four words in, the caller speaks.
        await voice.start(4)
        await call.send(VADUserStartedSpeakingFrame())
        await until(lambda: len(call.frames(InterruptionFrame)) == 1, "the interruption")
        await call.send(*caller_says("Actually, I have a question.")[1:])
        await until(lambda: voice.waiting == 1, "the next reply")
        await voice.play_all()
        await processor.hang_up()
    await drain_call_records()

    assert [row[8] for row in stored(owner_engine)] == [
        f"agent: {GREETING}\n"
        "caller: When are you open?\n"
        "agent: We are open Monday\n"
        "caller: Actually, I have a question.\n"
        "agent: Sure, what is it?"
    ]


async def test_the_end_does_not_wait_for_the_record(
    runtime: AsyncEngine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    released = threading.Event()

    def held(text: str) -> str:
        released.wait(timeout=10)
        return text

    monkeypatch.setattr(call_record, "_scrub", held)
    agent = await agent_for(ScriptedModel(), deps_on(runtime), register_tools())
    try:
        # The end returns while the record is still being masked.
        await asyncio.wait_for(agent.end(Reason.CALLER_HUNG_UP), timeout=5)
        assert agent.state.phase is Phase.ENDED
        assert stored(owner_engine) == []
    finally:
        released.set()
    await drain_call_records()
    assert [row[0] for row in stored(owner_engine)] == [agent.state.call_id]

    # A second end of the ended call starts no second record.
    caplog.set_level(logging.INFO)
    caplog.clear()
    await agent.end(Reason.CALLER_HUNG_UP)
    await drain_call_records()
    assert [r.getMessage() for r in caplog.records if r.name == "app.agent.call_record"] == []


async def test_a_record_that_cannot_start_never_reaches_the_call(
    runtime: AsyncEngine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("no loop for the record")

    monkeypatch.setattr(app.agent.turn, "record_call", broken)
    caplog.set_level(logging.INFO)
    agent = await agent_for(ScriptedModel(), deps_on(runtime), register_tools())
    await agent.end(Reason.CALLER_HUNG_UP)
    await drain_call_events()

    assert agent.state.phase is Phase.ENDED
    assert [r.getMessage() for r in caplog.records if r.name == "app.agent.turn"] == [
        f"call_record_not_started call_id={agent.state.call_id} error=RuntimeError"
    ]
    assert "no loop" not in caplog.text
