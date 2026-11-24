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
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from test_admin_api import admin_settings
from test_turn import (
    DIGITS_IN_WORDS,
    FIRST_PAIR,
    GREETING,
    SARAH_CLAIM,
    SECOND_PAIR,
    SEED_DAY,
    SSN_ASK,
    T0,
    ScriptedModel,
    agent_for,
    calls,
    deps_on,
    everything_sent,
    new_call_id,
    says,
)

from app.agent import call_record
from app.agent.call_record import drain_call_records, record_call
from app.agent.endings import END_PHASES, drain_call_events, end_call
from app.agent.state import CallState, Phase, move, new_call
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import CALL_CONNECTED
from app.audit.log import Reason
from app.db.models import CALL_END_REASONS, MAX_TRANSCRIPT
from app.db.seed import load_seed_data, seed
from app.db.session import create_database_engine
from app.guard.input import GuardAction
from app.guard.mask import mask
from app.main import create_app

pytestmark = pytest.mark.anyio

TURN_MODULE = REPO_ROOT / "app" / "agent" / "turn.py"

STORED = text(
    "SELECT call_id, started_at, ended_at, office_open, verified, patient_id, end_reason,"
    " turn_count, transcript, transcript_truncated FROM call_sessions ORDER BY id"
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


def turn_path_uses(source: str) -> list[str]:
    uses = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and "call_record" in (node.module or ""):
            uses.append(f"from {node.module}")
        elif isinstance(node, ast.ImportFrom | ast.Import):
            uses += [f"import {a.name}" for a in node.names if "call_record" in a.name]
        elif isinstance(node, ast.Name) and node.id in ("record_call", "call_record"):
            uses.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in ("record_call", "call_record"):
            uses.append(f".{node.attr}")
    return uses


# --- R8 ---------------------------------------------------------------------------------


async def test_transcript_is_masked_and_late(
    deps: Deps, owner_engine: Engine, presidio: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    model = ScriptedModel(calls(("verify_identity", SARAH_CLAIM)), says(CONTACT_REPLY))
    agent = agent_for(model, deps, register_tools())
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
    assert row[9] is False
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
    assert recorded == [f"call_recorded call_id={ended.call_id} turns=3 truncated=False"] * 2
    for raw in ("Sarah", "Johnson", PHONE, EMAIL, SSN_DIGITS, DIGITS_IN_WORDS, CONTACT_REPLY):
        assert raw not in caplog.text, raw

    # Never on the turn path: the turn module neither imports the record nor calls it.
    assert turn_path_uses("from app.agent.call_record import record_call\n")
    assert turn_path_uses("from app.agent import call_record\ncall_record.record_call(s)\n")
    assert turn_path_uses("import app.agent.call_record\n")
    assert turn_path_uses(TURN_MODULE.read_text(encoding="utf-8")) == []


async def test_each_swallowed_turn_leaves_one_marker(
    deps: Deps, owner_engine: Engine, plain_presidio: None
) -> None:
    model = ScriptedModel(calls(("verify_identity", SARAH_CLAIM)))
    agent = agent_for(model, deps, register_tools())
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
        "agent: Thank you, Sarah. You're verified. How can I help you today?",
    ]
    # Reading the record changed nothing the model is given.
    assert [request.messages for request in model.requests] == sent
    assert "withheld" not in everything_sent(model)


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
        {"role": "user", "content": CALL_CONNECTED},
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
            "agent: How can I help?\n"
            "caller: I'm Jane Roe, born ****-**-14, call ***-***-7788.\n"
            "agent: Let me check.\n"
            "agent: Your balance is twenty dollars.",
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
