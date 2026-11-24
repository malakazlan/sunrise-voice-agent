"""Per-turn timings: each caller turn's row handed to a bounded queue and written in batches by
one background task, never on the call's time. Nothing here reaches a vendor: the agent is
scripted or faked, and speech in and out are the test's own."""

import asyncio
import gc
import inspect
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest
from conftest import offline_settings
from pipecat.frames.frames import VADUserStartedSpeakingFrame
from speech_fakes import (
    CallPipeline,
    FakeTime,
    FakeVoice,
    agent_call,
    caller_says,
    settle,
    until,
)
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_admin_api import admin_settings, closed_port
from test_call_record import CONTACT, CONTACT_REPLY, PHONE, SSN_DIGITS
from test_turn import (
    CLAIM_TURN,
    DIGITS_IN_WORDS,
    SARAH_CLAIM,
    SEED_DAY,
    SSN_ASK,
    VERIFIED_SARAH,
    agent_for,
    deps_on,
    new_call_id,
)

from app.agent.scripts import ScriptId
from app.agent.state import Phase
from app.agent.tools import register_tools
from app.agent.turn import Line, Speak, TurnResult, TurnTimings
from app.agent.turn_record import QUEUE_ROWS, TurnRecorder, TurnRow
from app.audit.log import Reason
from app.db.models import TURN_PHASES
from app.db.seed import load_seed_data, seed
from app.db.session import create_database_engine, create_session_factory
from app.guard.input import GuardAction
from app.main import create_app
from app.voice.pipeline import AgentProcessor
from app.voice.speech import Playback
from evals.scripted import ScriptedModel, calls, says

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
CALL_ID = "0f1e2d3c4b5a46978a1b2c3d4e5f6a7b"
# The most a turn's hand-over may take at the call site.
CALL_SITE_SECONDS = 0.001

STORED = text(
    "SELECT call_id, turn, at, phase, verified, patient_id, guard_action, last_tool,"
    " tool_rounds, vad_ms, stt_ms, first_token_ms, last_token_ms, first_audio_ms, total_ms,"
    " dropped_before FROM call_turns ORDER BY id"
)
RECORD_US = text("SELECT record_us FROM call_turns ORDER BY id")


def turn_row(turn: int = 1, call_id: str = CALL_ID, **changes: Any) -> TurnRow:
    values: dict[str, Any] = {
        "call_id": call_id,
        "turn": turn,
        "phase": Phase.INTAKE,
        "patient_id": None,
        "guard_action": GuardAction.PASS,
        "last_tool": None,
        "tool_rounds": 0,
        "vad_ms": 500,
        "stt_ms": 120,
        "first_token_ms": 300,
        "last_token_ms": 900,
        "first_audio_ms": 1500,
        "total_ms": 1400,
    }
    values.update(changes)
    return TurnRow(**values)


def stored(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(STORED)]


def recorder_on(engine: AsyncEngine, **options: Any) -> TurnRecorder:
    return TurnRecorder(create_session_factory(engine), lambda: T0, **options)


@pytest.fixture
async def down() -> AsyncIterator[AsyncEngine]:
    """An engine whose server is not there: nothing listens on its port."""
    engine = create_database_engine(
        offline_settings(postgres_host="127.0.0.1", postgres_port=closed_port())
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def stalled(silent_server: int) -> AsyncIterator[AsyncEngine]:
    """An engine whose server takes the connection and never answers."""
    engine = create_database_engine(
        offline_settings(postgres_host="127.0.0.1", postgres_port=silent_server)
    )
    try:
        yield engine
    finally:
        await engine.dispose()


class Says:
    """A call's agent that says one line a turn (none for a swallowed turn) and reports the
    turn as the real agent does: its action, its state and its tool."""

    ending: Reason | None = None

    def __init__(self, *, action: GuardAction = GuardAction.PASS, linger: bool = False) -> None:
        self.action = action
        # Whether the turn goes on after its line is released, long enough for it to play.
        self.linger = linger
        self.ended: Reason | None = None
        self._ids = iter(range(1, 1000))

    async def handle(self, transcript: str, speak: Speak | None = None) -> TurnResult:
        lines: tuple[str, ...] = ()
        if self.action is GuardAction.PASS and speak is not None:
            speak(Line(id=next(self._ids), text="One moment.", script=None))
            lines = ("One moment.",)
        if self.linger:
            await settle()
        return TurnResult(
            action=self.action,
            shown=None,
            lines=lines,
            hand_off=False,
            timings=TurnTimings(first_token_ms=40, last_token_ms=90, tool_rounds=1),
            withheld=None,
            phase=Phase.SERVING,
            patient_id="P001",
            last_tool="get_lab_status",
        )

    async def prompt(self, script: ScriptId, speak: Speak) -> Line:
        raise AssertionError("no line is asked for")

    def heard(self, line_id: int, words: int | None = None) -> None:
        return None

    async def end(self, reason: Reason) -> None:
        self.ended = reason

    def is_emergency(self, transcript: str) -> bool:
        return False

    async def say_emergency(self, speak: Speak) -> Line:
        raise AssertionError("no emergency is said")


class Kept(TurnRecorder):
    """A recorder that keeps the rows handed to it, for the tests of the adapter."""

    def __init__(self) -> None:
        # Sessions bound to no database: nothing is written.
        super().__init__(async_sessionmaker(), now)
        self.rows: list[TurnRow] = []

    def put(self, row: TurnRow) -> None:
        self.rows.append(row)


def now() -> datetime:
    return T0


def adapter(agent: Says, turns: TurnRecorder, time_: FakeTime) -> tuple[AgentProcessor, Playback]:
    async def start() -> tuple[Says, Line]:
        return agent, Line(id=0, text="Hello.", script=ScriptId.GREETING)

    playback = Playback()
    processor = AgentProcessor(
        CALL_ID,
        start,
        playback,
        idle_seconds=30,
        max_seconds=600,
        vad_stop_seconds=0.5,
        clock=time_.clock,
        sleep=time_.sleep,
        turns=turns,
    )
    return processor, playback


def has_turns(call: CallPipeline, count: int) -> bool:
    return len([m for m in call.messages() if m["type"] == "turn"]) == count


def said_last(voice: FakeVoice, line: str) -> bool:
    return voice.spoken[-1][0] == line


def put_times(recorder: TurnRecorder, count: int, first_turn: int = 1) -> list[float]:
    """Each hand-over's time at the call site, in seconds. The garbage collector is held off
    while they run: a collection's pause belongs to whatever code it lands in, not to this."""
    times = []
    gc.disable()
    try:
        for turn in range(first_turn, first_turn + count):
            row = turn_row(turn)
            started = time.perf_counter()
            recorder.put(row)
            times.append(time.perf_counter() - started)
    finally:
        gc.enable()
    return times


# --- R1 -----------------------------------------------------------------------------------


async def test_recording_never_touches_the_call(
    down: AsyncEngine, stalled: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    # The hand-over is a plain function: the call has nothing to await.
    assert not inspect.iscoroutinefunction(TurnRecorder.put)

    # The database down, stalled, and the queue full behind a stalled writer: every hand-over
    # returns at once, raises nothing, and the writer never holds the call.
    recorders = {
        "down": recorder_on(down, write_seconds=0.2),
        "stalled": recorder_on(stalled, write_seconds=0.2),
        "full": recorder_on(stalled, capacity=3, write_seconds=0.2),
    }
    for name, recorder in recorders.items():
        times: list[float] = []
        for start in range(1, 200, 10):
            times += put_times(recorder, 10, start)
            # The writer gets its turn on the event loop between hand-overs, as on a call.
            await asyncio.sleep(0)
        assert max(times) < CALL_SITE_SECONDS, (name, max(times))
    assert recorders["full"].dropped > 0

    # A clock that fails, or anything else that goes wrong inside the hand-over, never reaches
    # the call: the row is dropped and counted.
    def broken() -> datetime:
        raise ValueError("no clock")

    failing = TurnRecorder(create_session_factory(down), broken)
    failing.put(turn_row())
    assert failing.dropped == 1

    # A call whose recorder cannot write runs every turn as it would without one.
    agent = Says()
    time_ = FakeTime()
    processor, playback = adapter(agent, recorders["down"], time_)
    async with CallPipeline(processor, playback, FakeVoice()) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        for number in range(1, 6):
            await call.send(*caller_says(f"Question number {number}."))
            await until(partial(has_turns, call, number), "the turn")
        await processor.hang_up()
    assert agent.ended is Reason.CALLER_HUNG_UP
    assert [m["turn"] for m in call.messages() if m["type"] == "first_audio"] == [1, 2, 3, 4, 5]

    for recorder in recorders.values():
        await recorder.drain()
    assert "call_turns_write_failed" in caplog.text
    # Nothing of what was said, or of the failure's text, is logged.
    assert "Question" not in caplog.text
    assert "no clock" not in caplog.text


# --- R2 -----------------------------------------------------------------------------------


async def test_full_queue_drops_and_counts(
    stalled: AsyncEngine,
    runtime: AsyncEngine,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    # The queue holds 1,000 rows. Handed over without a pause, the 1,001st and the 1,002nd are
    # dropped; the first of them is logged, once the hand-over has returned.
    assert QUEUE_ROWS == 1000
    behind = recorder_on(stalled, write_seconds=0.2)
    times = put_times(behind, 1002)
    assert behind.dropped == 2
    assert max(times) < CALL_SITE_SECONDS
    await asyncio.sleep(0)
    dropped = [r.getMessage() for r in caplog.records if "call_turn_dropped" in r.getMessage()]
    assert dropped == [f"call_turn_dropped call_id={CALL_ID} turn=1001 dropped=1"]
    await behind.drain()

    # The count goes with the rows written after it, so the console reads it from them.
    recorder = recorder_on(runtime, capacity=3)
    put_times(recorder, 5)
    assert recorder.dropped == 2
    await recorder.drain()
    recorder.put(turn_row(6))
    await recorder.drain()
    assert [(row[1], row[15]) for row in stored(owner_engine)] == [(1, 0), (2, 0), (3, 0), (6, 2)]


# --- R3 -----------------------------------------------------------------------------------

# Every column of call_turns, its type and whether it may be null; and the rule each text column
# is held to: a call id, a patient id, or a registry's names.
TURN_COLUMNS = text(
    "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
    " WHERE table_schema = 'public' AND table_name = 'call_turns'"
)
TURN_CHECKS = text(
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
    " WHERE conrelid = 'call_turns'::regclass AND contype = 'c'"
)


async def test_turn_rows_hold_no_text(
    runtime: AsyncEngine, owner_engine: Engine, owner_session: AsyncSession
) -> None:
    # The schema: numbers, booleans, one instant, and text only where a CHECK holds it to an
    # id's shape or a registry's names.
    with owner_engine.connect() as connection:
        columns = {row[0]: (row[1], row[2]) for row in connection.execute(TURN_COLUMNS)}
        checks = {str(row[0]) for row in connection.execute(TURN_CHECKS)}
    assert columns == {
        "id": ("bigint", "NO"),
        "call_id": ("text", "NO"),
        "turn": ("integer", "NO"),
        "at": ("timestamp with time zone", "NO"),
        "phase": ("text", "NO"),
        "verified": ("boolean", "NO"),
        "patient_id": ("text", "YES"),
        "guard_action": ("text", "NO"),
        "last_tool": ("text", "YES"),
        "tool_rounds": ("integer", "NO"),
        "vad_ms": ("integer", "YES"),
        "stt_ms": ("integer", "YES"),
        "first_token_ms": ("integer", "YES"),
        "last_token_ms": ("integer", "YES"),
        "first_audio_ms": ("integer", "YES"),
        "total_ms": ("integer", "NO"),
        "record_us": ("integer", "NO"),
        "dropped_before": ("integer", "NO"),
    }
    assert {
        "CHECK ((call_id ~ '^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'::text))",
        "CHECK ((patient_id ~ '^P[0-9]{3,6}$'::text))",
        "CHECK ((phase = ANY (ARRAY['greeting'::text, 'intake'::text, 'serving'::text,"
        " 'booking'::text, 'emergency'::text, 'handoff'::text, 'closing'::text,"
        " 'ended'::text])))",
        "CHECK ((guard_action = ANY (ARRAY['pass'::text, 'swallow'::text, 'emergency'::text])))",
        "CHECK ((last_tool ~ '^[a-z_]{2,40}$'::text))",
    } <= checks
    assert set(TURN_PHASES) == {phase.value for phase in Phase}

    # A call with an identifier in every turn: a name and a date of birth, the last four, a
    # phone number and an email address.
    await seed(owner_session, load_seed_data(), SEED_DAY)
    await owner_session.commit()
    recorder = recorder_on(runtime)
    model = ScriptedModel(calls(("verify_identity", SARAH_CLAIM)), says(CONTACT_REPLY))
    time_ = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(call_id, deps_on(runtime), model, time_, turns=recorder)
    voice = FakeVoice()
    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the greeting")
        for said, line in ((CLAIM_TURN, SSN_ASK), (DIGITS_IN_WORDS, VERIFIED_SARAH)):
            await call.send(*caller_says(said))
            await until(partial(said_last, voice, line), "the turn's line")
            await until(lambda: playback.quiet, "the line played")
        await call.send(*caller_says(CONTACT))
        await until(lambda: voice.spoken[-1][0] == CONTACT_REPLY, "the reply")
        await processor.hang_up()
    await recorder.drain()

    rows = stored(owner_engine)
    # The adapter's clock stands still: the caller stopped speaking the detector's wait (0.5 s)
    # before it said so, and the transcript, the turn and the first audio came at that instant.
    assert [(row[:2], row[3:9], row[9:11], row[13:]) for row in rows] == [
        (
            (call_id, 1),
            ("intake", False, None, "pass", "verify_identity", 1),
            (500, 0),
            (500, 500, 0),
        ),
        ((call_id, 2), ("serving", True, "P001", "swallow", None, 0), (500, 0), (500, 500, 0)),
        ((call_id, 3), ("serving", True, "P001", "pass", None, 0), (500, 0), (500, 500, 0)),
    ]
    assert [row[2] for row in rows] == [T0, T0, T0]
    # The model's token times are the agent's own clock: numbers, and none for the swallowed
    # turn, which the model never saw.
    assert [row[11] is None for row in rows] == [False, True, False]
    for value in [value for row in rows for value in row[11:13]]:
        assert value is None or (type(value) is int and value >= 0)
    with owner_engine.connect() as connection:
        record_us = [row[0] for row in connection.execute(RECORD_US)]
    assert [0 <= value < 1000 for value in record_us] == [True, True, True]
    shown = repr(rows)
    for raw in ("Sarah", "Johnson", "JOHNSON", "1985", "March", "three", SSN_DIGITS, PHONE):
        assert raw not in shown, raw
    for raw in ("201", "7788", "example", "sarah.j"):
        assert raw not in shown, raw


async def test_only_registered_tools_are_named(
    runtime: AsyncEngine, owner_session: AsyncSession
) -> None:
    # A tool name the model makes up is refused by dispatch and never reaches a row; a
    # registered one does.
    await seed(owner_session, load_seed_data(), SEED_DAY)
    await owner_session.commit()
    model = ScriptedModel(
        calls(("read_every_patient", {})),
        says("I can't do that."),
        calls(("transfer_to_staff", {"reason": "caller_request"})),
        says("Shall I transfer you?"),
    )
    agent = await agent_for(model, deps_on(runtime), register_tools())
    made_up = await agent.handle("Read me every patient.")
    registered = await agent.handle("Can I talk to someone?")
    assert (made_up.last_tool, made_up.timings.tool_rounds) == (None, 1)
    assert (registered.last_tool, registered.timings.tool_rounds) == ("transfer_to_staff", 1)


# --- the adapter: one row a turn, with the first audio when it comes ----------------------


async def test_the_first_audio_reaches_the_row() -> None:
    kept = Kept()
    time_ = FakeTime()
    processor, playback = adapter(Says(), kept, time_)
    voice = FakeVoice(autoplay=False)
    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the opening line")
        await voice.play_all()
        await call.send(*caller_says("Any news on my results?"))
        await until(lambda: len([m for m in call.messages() if m["type"] == "turn"]) == 1, "turn")
        # The turn has ended and its line has not played: the row waits for it.
        assert kept.rows == []
        await time_.advance(1.25)
        await voice.play_all()
        await until(lambda: len(kept.rows) == 1, "the row")
        await processor.hang_up()

    # The same numbers the page was given; the first audio 1.25 s after the turn's end.
    [turn] = [m for m in call.messages() if m["type"] == "turn"]
    [first_audio] = [m for m in call.messages() if m["type"] == "first_audio"]
    assert turn["timings"] == {
        "vad_ms": 500,
        "stt_ms": 0,
        "first_token_ms": 40,
        "last_token_ms": 90,
        "tool_rounds": 1,
        "total_ms": 500,
    }
    assert first_audio == {"type": "first_audio", "turn": 1, "first_audio_ms": 1750}
    assert kept.rows == [
        turn_row(
            1,
            phase=Phase.SERVING,
            patient_id="P001",
            last_tool="get_lab_status",
            tool_rounds=1,
            stt_ms=0,
            first_token_ms=40,
            last_token_ms=90,
            first_audio_ms=1750,
            total_ms=500,
        )
    ]


async def test_audio_before_the_turn_ends_reaches_the_row() -> None:
    # A line that plays while its turn is still running (a filler, a scripted line): its first
    # audio is kept until the turn's row is made.
    kept = Kept()
    agent = Says(linger=True)
    processor, playback = adapter(agent, kept, FakeTime())
    voice = FakeVoice()
    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        await call.send(*caller_says("Any news on my results?"))
        await until(lambda: len(kept.rows) == 1, "the row")
        await processor.hang_up()
    messages = [m["type"] for m in call.messages() if m["type"] in ("turn", "first_audio")]
    assert messages == ["first_audio", "turn"]
    assert [(row.turn, row.first_audio_ms) for row in kept.rows] == [(1, 500)]


async def test_a_row_does_not_wait_for_audio_that_will_not_come() -> None:
    kept = Kept()
    time_ = FakeTime()
    processor, playback = adapter(Says(), kept, time_)
    voice = FakeVoice(autoplay=False)
    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the opening line")
        await voice.play_all()
        # The caller speaks over the turn's line before it plays: the line is over unheard.
        await call.send(*caller_says("First question."))
        await until(lambda: len([m for m in call.messages() if m["type"] == "turn"]) == 1, "turn")
        await call.send(VADUserStartedSpeakingFrame())
        await until(lambda: len(kept.rows) == 1, "the first row")
        # The next turn's line never plays either: the call ends first.
        await call.send(*caller_says("Second question.")[1:])
        await until(lambda: len([m for m in call.messages() if m["type"] == "turn"]) == 2, "turn")
        assert len(kept.rows) == 1
        await processor.hang_up()

    assert [(row.turn, row.first_audio_ms) for row in kept.rows] == [(1, None), (2, None)]


async def test_a_turn_without_a_line_is_recorded_at_once() -> None:
    kept = Kept()
    processor, playback = adapter(Says(action=GuardAction.SWALLOW), kept, FakeTime())
    async with CallPipeline(processor, playback, FakeVoice()) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        await call.send(*caller_says("three three"))
        await until(lambda: len(kept.rows) == 1, "the row")
        await processor.hang_up()
    assert [(row.guard_action, row.first_audio_ms) for row in kept.rows] == [
        (GuardAction.SWALLOW, None)
    ]


async def test_shutdown_waits_for_turn_rows(database: Any, owner_engine: Engine) -> None:
    app = create_app(admin_settings(database))
    async with app.router.lifespan_context(app):
        recorder: TurnRecorder = app.state.turns
        recorder.put(turn_row(call_id=new_call_id()))
    assert [row[1] for row in stored(owner_engine)] == [1]
    # Stamped with the call's clock: the clinic's, as the audit log's events are.
    [at] = [row[2] for row in stored(owner_engine)]
    assert abs(at - datetime.now(UTC)) < timedelta(minutes=1)
