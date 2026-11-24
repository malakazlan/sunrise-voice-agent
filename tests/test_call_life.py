"""A call's life on the voice adapter: how it starts, every way it ends, its timers and the
filler line. The model is scripted, text-to-speech and the transport's output are a fake that
plays lines when the test says (tests/speech_fakes.py), and the timers run on a clock the test
moves. The call's events are read back from the database."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, cast

import pytest
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InterruptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from speech_fakes import CallPipeline, FakeTime, FakeVoice, agent_call, caller_says, settle, until
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_turn import (
    EMERGENCY,
    EVENING,
    GREETING,
    SEED_DAY,
    T0,
    deps_on,
    new_call_id,
)

from app.agent.endings import drain_call_events
from app.agent.model import ModelReply
from app.agent.state import IdentityClaim, Phase, claim, move, new_call, verify
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import CallAgent, Line
from app.db.seed import load_seed_data, seed
from evals.scripted import ScriptedModel, calls, says

pytestmark = pytest.mark.anyio

GOODBYE = "Thank you for calling Sunrise Family Clinic. Goodbye."
TRANSFER = "I'm transferring you to our staff now. Please hold."
STILL_THERE = "Are you still there?"
FILLER = "One moment while I check."
AFTER_HOURS_CLOSE = (
    "Thank you, Ana Diaz. Your message has been recorded, and our team will get back to you on"
    " the next business day. If this is an emergency, please hang up and call 911. Have a good"
    " night."
)
MESSAGE = {
    "name": "Ana Diaz",
    "callback_number": "555-410-2233",
    "reason_short": "a question about my bill",
    "confirmed": True,
}

CALL_EVENTS = text(
    "SELECT action, reason FROM audit_events"
    " WHERE call_id = :call_id AND action IN ('call_started', 'call_ended') ORDER BY seq"
)


def call_events(engine: Engine, call_id: str) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(CALL_EVENTS, {"call_id": call_id})]


def texts(voice: FakeVoice) -> list[str]:
    return [spoken for spoken, _interruptible in voice.spoken]


def ended_reason(call: CallPipeline) -> list[str]:
    return [m["reason"] for m in call.messages() if m["type"] == "ended"]


# --- R4 ---------------------------------------------------------------------------------

# What the caller says, and what the model does with it, for the endings a turn decides.
TURNS: dict[str, tuple[str, list[ModelReply]]] = {
    "goodbye": ("No, that's all. Thanks.", [calls(("close_call", {}))]),
    "emergency": ("I think I'm having a heart attack", []),
    "transfer": (
        "Yes, please put me through to someone.",
        [calls(("transfer_to_staff", {"reason": "caller_request", "confirmed": True}))],
    ),
    "after_hours_close": ("Yes, that's right, please send it.", [calls(("take_message", MESSAGE))]),
    "hang_up_during_goodbye": ("No, that's all. Thanks.", [calls(("close_call", {}))]),
}
# Each ending, the reason its call_ended event gives and the last line said, written by hand.
ENDINGS = {
    "goodbye": ("completed", GOODBYE),
    "emergency": ("emergency", EMERGENCY),
    "transfer": ("transferred", TRANSFER),
    "after_hours_close": ("completed", AFTER_HOURS_CLOSE),
    "hang_up": ("caller_hung_up", GREETING),
    "idle": ("idle_timeout", GOODBYE),
    "max_duration": ("max_duration", GOODBYE),
    "error": ("error", GREETING),
    # The goodbye is playing when the caller hangs up: the call had already been completed.
    "hang_up_during_goodbye": ("completed", GOODBYE),
}


@pytest.mark.parametrize("ending", list(ENDINGS))
async def test_every_ending(
    runtime: AsyncEngine, owner_engine: Engine, caplog: pytest.LogCaptureFixture, ending: str
) -> None:
    caplog.set_level(logging.INFO)
    deps = deps_on(runtime, EVENING if ending == "after_hours_close" else T0)
    caller, steps = TURNS.get(ending, ("", []))
    model = ScriptedModel(*steps)
    time = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(call_id, deps, model, time)
    hangs_up = ending.startswith("hang_up")
    voice = FakeVoice(autoplay=ending != "hang_up_during_goodbye")

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: len(voice.spoken) == 1, "the opening line")
        await voice.play_all()
        # The idle timer and the call's maximum are waiting.
        await until(lambda: time.waiting == 2, "the timers")
        if caller:
            await call.send(*caller_says(caller))
        if ending == "hang_up_during_goodbye":
            await until(lambda: voice.waiting == 1, "the goodbye")
            await voice.start(3)
            await processor.hang_up()
        elif caller:
            pass
        elif ending == "hang_up":
            await processor.hang_up()
        elif ending == "idle":
            await time.advance(30)
            await until(lambda: texts(voice)[-1] == STILL_THERE, "the idle prompt")
            await until(lambda: time.waiting == 2, "the idle timer again")
            await time.advance(30)
        elif ending == "max_duration":
            await time.advance(600)
        else:
            # The pipeline ends without the call having decided to: a processor that can no
            # longer work ends it this way.
            await call.worker.queue_frame(EndFrame())
        if not hangs_up:
            await until(lambda: call.ended, "the call to end")

    reason, last_line = ENDINGS[ending]
    await drain_call_events()
    assert call_events(owner_engine, call_id) == [("call_started", None), ("call_ended", reason)]
    assert texts(voice)[-1] == last_line
    if not hangs_up and ending != "error":
        assert ended_reason(call) == [reason]
    # A second ending changes nothing: the call has ended, once.
    await processor.hang_up()
    await drain_call_events()
    assert call_events(owner_engine, call_id)[-1] == ("call_ended", reason)
    assert [r.getMessage() for r in caplog.records if "call_life_ended" in r.getMessage()] == [
        f"call_life_ended call_id={call_id} reason={reason}"
    ]


# --- R5 ---------------------------------------------------------------------------------


async def test_emergency_plays_then_ends(runtime: AsyncEngine, owner_engine: Engine) -> None:
    model = ScriptedModel()
    time = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(call_id, deps_on(runtime), model, time)
    voice = FakeVoice(autoplay=False)

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the greeting")
        await voice.play_all()
        await call.send(*caller_says("I think I'm having a heart attack"))
        await until(lambda: voice.waiting == 1, "the emergency line")

        # Half of it has played, and the caller is talking: the call goes on.
        await voice.start(8)
        await call.send(*caller_says("What? Wait."))
        await call.send(VADUserStartedSpeakingFrame())
        await drain_call_events()
        assert not call.ended
        assert call_events(owner_engine, call_id) == [("call_started", None)]
        assert ended_reason(call) == []

        # The whole line plays, and then the call ends.
        await voice.finish()
        await until(lambda: call.ended, "the call to end")

    assert voice.played[-len(EMERGENCY.split()) :] == EMERGENCY.split()
    assert model.requests == []
    await drain_call_events()
    assert call_events(owner_engine, call_id) == [
        ("call_started", None),
        ("call_ended", "emergency"),
    ]
    assert ended_reason(call) == ["emergency"]


# --- R6 ---------------------------------------------------------------------------------


async def test_idle_and_max_duration(runtime: AsyncEngine, owner_engine: Engine) -> None:
    model = ScriptedModel(says("Good. How can I help?"))
    time = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(call_id, deps_on(runtime), model, time)
    voice = FakeVoice(autoplay=False)

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the greeting")
        # Never during a line: the greeting plays on through any silence.
        await voice.start(2)
        await time.advance(100)
        assert texts(voice) == [GREETING]
        await voice.finish()

        await until(lambda: time.waiting == 2, "the idle timer")
        await time.advance(29.9)
        assert texts(voice) == [GREETING]
        await time.advance(0.1)
        await until(lambda: voice.waiting == 1, "the idle prompt")
        assert texts(voice) == [GREETING, STILL_THERE]
        await voice.play_all()

        # The caller answers: the count starts again, so the next silence is one prompt again.
        await call.send(*caller_says("Yes, I'm here."))
        await until(lambda: voice.waiting == 1, "the reply")
        # The turn is over and its reply is playing: still no timer runs.
        await voice.start(1)
        await time.advance(100)
        assert texts(voice)[-1] == "Good. How can I help?"
        await voice.finish()
        await until(lambda: time.waiting == 2, "the idle timer again")
        await time.advance(30)
        await until(lambda: voice.waiting == 1, "the second idle prompt")
        assert texts(voice)[-1] == STILL_THERE
        await voice.play_all()
        await until(lambda: time.waiting == 2, "the idle timer once more")
        await time.advance(30)
        await until(lambda: voice.waiting == 1, "the goodbye")
        assert texts(voice)[-1] == GOODBYE
        assert not call.ended
        await voice.play_all()
        await until(lambda: call.ended, "the call to end")

    assert texts(voice) == [
        GREETING,
        STILL_THERE,
        "Good. How can I help?",
        STILL_THERE,
        GOODBYE,
    ]
    await drain_call_events()
    assert call_events(owner_engine, call_id)[-1] == ("call_ended", "idle_timeout")

    # The call's maximum, reached while a line plays: the goodbye waits for it.
    model = ScriptedModel(says("Your appointment is on Thursday."))
    time = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(
        call_id, deps_on(runtime), model, time, idle_seconds=1000, max_seconds=600
    )
    voice = FakeVoice(autoplay=False)
    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the greeting")
        await voice.play_all()
        await time.advance(599)
        await call.send(*caller_says("When is my appointment?"))
        await until(lambda: voice.waiting == 1, "the reply")
        await voice.start(2)
        await time.advance(1)
        assert voice.waiting == 0
        assert texts(voice)[-1] == "Your appointment is on Thursday."
        await voice.finish()
        await until(lambda: voice.waiting == 1, "the goodbye")
        assert texts(voice)[-1] == GOODBYE
        await voice.play_all()
        await until(lambda: call.ended, "the call to end")

    await drain_call_events()
    assert call_events(owner_engine, call_id)[-1] == ("call_ended", "max_duration")


# --- R7 ---------------------------------------------------------------------------------


@pytest.fixture
async def seeded_deps(runtime: AsyncEngine, owner_session: AsyncSession) -> Deps:
    await seed(owner_session, load_seed_data(), SEED_DAY)
    await owner_session.commit()
    return deps_on(runtime)


class SlowSessions:
    """Sessions that each take `seconds` of the fake clock to open, every timer due by then
    firing meanwhile: a slow tool."""

    def __init__(self, sessions: Callable[[], AsyncSession], time: FakeTime, seconds: float):
        self._sessions = sessions
        self._time = time
        self._seconds = seconds

    def __call__(self) -> "SlowSession":
        return SlowSession(self._sessions(), self._time, self._seconds)


class SlowSession:
    def __init__(self, session: AsyncSession, time: FakeTime, seconds: float) -> None:
        self._session = session
        self._time = time
        self._seconds = seconds

    async def __aenter__(self) -> AsyncSession:
        await self._time.advance(self._seconds)
        return await self._session.__aenter__()

    async def __aexit__(self, *exc: Any) -> None:
        await self._session.__aexit__(*exc)


def held(
    reply: ModelReply,
) -> tuple[asyncio.Event, Callable[[asyncio.Event], Awaitable[ModelReply]]]:
    """A reply that comes only once the test lets it."""
    let_go = asyncio.Event()

    async def step(first_token: asyncio.Event) -> ModelReply:
        await let_go.wait()
        first_token.set()
        return reply

    return let_go, step


def serving(call_id: str) -> Any:
    return verify(
        move(new_call(call_id=call_id, started_at=T0, office_open=True), Phase.INTAKE),
        patient_id="P001",
        first_name="Sarah",
    )


async def test_filler_rules(seeded_deps: Deps) -> None:
    registry = register_tools()
    balance = "You owe forty-five dollars."

    async def run(
        state: Any, deps: Deps, model: ScriptedModel, time: FakeTime, caller: str
    ) -> tuple[list[str], "asyncio.Task[Any]"]:
        spoken: list[str] = []
        agent = CallAgent(state, deps, model, registry, clock=time.clock, sleep=time.sleep)
        turn = asyncio.ensure_future(agent.handle(caller, lambda line: spoken.append(line.text)))
        return spoken, turn

    # A follow-up that takes longer than the delay: the filler, once, before the answer.
    time = FakeTime()
    let_go, step = held(says(balance))
    model = ScriptedModel(calls(("get_balance", {})), step)
    spoken, turn = await run(serving(new_call_id()), seeded_deps, model, time, "What do I owe?")
    await until(lambda: len(model.requests) == 2, "the follow-up request")
    await time.advance(1.1)
    assert spoken == []
    await time.advance(0.1)
    assert spoken == [FILLER]
    let_go.set()
    assert (await turn).lines == (FILLER, balance)

    # An answer within the delay: no filler.
    time = FakeTime()
    let_go, step = held(says(balance))
    model = ScriptedModel(calls(("get_balance", {})), step)
    spoken, turn = await run(serving(new_call_id()), seeded_deps, model, time, "What do I owe?")
    await until(lambda: len(model.requests) == 2, "the follow-up request")
    await time.advance(1.1)
    let_go.set()
    assert (await turn).lines == (balance,)
    await time.advance(5)
    assert spoken == [balance]

    # Two slow rounds in one turn: one filler.
    time = FakeTime()
    first, step_one = held(calls(("get_balance", {})))
    second, step_two = held(says(balance))
    model = ScriptedModel(calls(("get_balance", {})), step_one, step_two)
    spoken, turn = await run(serving(new_call_id()), seeded_deps, model, time, "What do I owe?")
    await until(lambda: len(model.requests) == 2, "the second request")
    await time.advance(1.2)
    first.set()
    await until(lambda: len(model.requests) == 3, "the third request")
    await time.advance(5)
    second.set()
    assert (await turn).lines == (FILLER, balance)

    # A slow tool with a scripted answer: the line, never a filler before it. The same slowness
    # with an answer the model gives: the filler as soon as the tool is done.
    time = FakeTime()
    slow = Deps(
        sessions=cast(
            "async_sessionmaker[AsyncSession]", SlowSessions(seeded_deps.sessions, time, 5)
        ),
        audit=seeded_deps.audit,
        now=seeded_deps.now,
        claim_keys=seeded_deps.claim_keys,
    )
    model = ScriptedModel(calls(("close_call", {})))
    spoken, turn = await run(serving(new_call_id()), slow, model, time, "That's all.")
    assert (await turn).lines == (GOODBYE,)
    assert spoken == [GOODBYE]
    let_go, step = held(says(balance))
    model = ScriptedModel(calls(("get_balance", {})), step)
    spoken, turn = await run(serving(new_call_id()), slow, model, time, "What do I owe?")
    await until(lambda: spoken == [FILLER], "the filler")
    let_go.set()
    assert (await turn).lines == (FILLER, balance)

    # The SSN step is armed: however slow the tool, no filler.
    time = FakeTime()
    slow = Deps(
        sessions=cast(
            "async_sessionmaker[AsyncSession]", SlowSessions(seeded_deps.sessions, time, 5)
        ),
        audit=seeded_deps.audit,
        now=seeded_deps.now,
        claim_keys=seeded_deps.claim_keys,
    )
    armed = claim(
        move(new_call(call_id=new_call_id(), started_at=T0, office_open=True), Phase.INTAKE),
        IdentityClaim(full_name="Sarah Johnson", dob=date(1985, 3, 14)),
    )
    let_go, step = held(says("Shall I transfer you to our staff?"))
    model = ScriptedModel(calls(("transfer_to_staff", {"reason": "caller_request"})), step)
    spoken, turn = await run(armed, slow, model, time, "Can I talk to a person instead?")
    await until(lambda: len(model.requests) == 2, "the follow-up request")
    await time.advance(5)
    await settle()
    assert spoken == []
    let_go.set()
    assert (await turn).lines == ("Shall I transfer you to our staff?",)


# --- the emergency, heard whatever the call is doing ------------------------------------

CHEST_PAIN = "Wait, I'm having chest pain"
HEART_ATTACK = "I think I'm having a heart attack"


def final(text: str) -> TranscriptionFrame:
    """A finalized transcript alone, as speech-to-text sends it once the caller has stopped."""
    return TranscriptionFrame(text, "", "2026-11-24T15:00:00Z", finalized=True)


async def ends_with_the_emergency(
    call: CallPipeline, voice: FakeVoice, owner_engine: Engine, call_id: str
) -> None:
    """The 911 line, then nothing after it, played in full; the call ends for the emergency."""
    await until(lambda: voice.waiting == 1, "the emergency line")
    assert voice.spoken[-1] == (EMERGENCY, False)
    assert not call.ended
    await voice.play_all()
    await until(lambda: call.ended, "the call to end")
    assert voice.spoken[-1] == (EMERGENCY, False)
    assert voice.played[-len(EMERGENCY.split()) :] == EMERGENCY.split()
    assert ended_reason(call) == ["emergency"]
    await drain_call_events()
    assert call_events(owner_engine, call_id) == [
        ("call_started", None),
        ("call_ended", "emergency"),
    ]


@pytest.mark.parametrize("closing", ["goodbye", "transfer", "after_hours_close"])
async def test_an_emergency_while_the_call_closes(
    runtime: AsyncEngine, owner_engine: Engine, closing: str
) -> None:
    caller, steps = TURNS[closing]
    model = ScriptedModel(*steps)
    time = FakeTime()
    call_id = new_call_id()
    deps = deps_on(runtime, EVENING if closing == "after_hours_close" else T0)
    processor, playback = agent_call(call_id, deps, model, time)
    voice = FakeVoice(autoplay=False)

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the opening line")
        await voice.play_all()
        await call.send(*caller_says(caller))
        await until(lambda: voice.waiting == 1, "the closing line")
        assert texts(voice)[-1] == ENDINGS[closing][1]
        # Three words in, the caller speaks over it: the line stops, and what they say is an
        # emergency. The call had decided to end; the emergency is heard all the same.
        await voice.start(3)
        await call.send(VADUserStartedSpeakingFrame())
        await until(lambda: len(call.frames(InterruptionFrame)) == 1, "the interruption")
        await call.send(*caller_says(CHEST_PAIN)[1:])
        await ends_with_the_emergency(call, voice, owner_engine, call_id)


async def test_an_emergency_while_a_turn_runs(runtime: AsyncEngine, owner_engine: Engine) -> None:
    let_go, slow = held(says("Your appointment is on Thursday."))
    model = ScriptedModel(slow)
    time = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(call_id, deps_on(runtime), model, time)
    voice = FakeVoice(autoplay=False)
    # Each turn of the event loop, counted; and the turn in which the transcript of the
    # emergency reaches the adapter, and the one in which its line is released.
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    arrived: list[int] = []
    released: list[int] = []
    hears = processor._caller
    releases = playback.release

    async def hearing(frame: Frame) -> None:
        if isinstance(frame, TranscriptionFrame):
            arrived.append(ticks)
        await hears(frame)

    def releasing(line: Line, *, opening: bool = False) -> None:
        if line.text == EMERGENCY:
            released.append(ticks)
        releases(line, opening=opening)

    processor._caller = hearing  # type: ignore[method-assign]
    playback.release = releasing  # type: ignore[method-assign]
    ticker = asyncio.ensure_future(tick())
    try:
        async with CallPipeline(processor, playback, voice) as call:
            processor.connected()
            await until(lambda: voice.waiting == 1, "the opening line")
            await voice.play_all()
            # Ten seconds before the call's maximum, a turn waits on the model; the caller
            # speaks again, and it is an emergency.
            await time.advance(590)
            await call.send(*caller_says("When is my appointment?"))
            await until(lambda: len(model.requests) == 1, "the model to be asked")
            await call.send(*caller_says(HEART_ATTACK))
            await until(lambda: released != [], "the emergency line")
            # Released in the same turn of the event loop as its transcript arrived: no wait
            # for the running turn, which is cancelled.
            assert released == [arrived[-1]]
            let_go.set()
            await time.advance(10)
            await ends_with_the_emergency(call, voice, owner_engine, call_id)
    finally:
        ticker.cancel()
    # The turn's own reply, too late, was never spoken; nor was a goodbye.
    assert "Your appointment is on Thursday." not in texts(voice)
    assert GOODBYE not in texts(voice)


@pytest.mark.parametrize("timer", ["idle", "max_duration"])
async def test_an_emergency_as_a_timer_fires(
    runtime: AsyncEngine, owner_engine: Engine, timer: str
) -> None:
    model = ScriptedModel()
    time = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(call_id, deps_on(runtime), model, time)
    voice = FakeVoice(autoplay=False)

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the opening line")
        await voice.play_all()
        await until(lambda: time.waiting == 2, "the timers")
        # The transcript and the timer, in the same instant.
        await call.worker.queue_frame(final(HEART_ATTACK))
        await time.advance(30 if timer == "idle" else 600)
        # Whatever the timer released before the 911 line was cut: the 911 line is the last.
        await ends_with_the_emergency(call, voice, owner_engine, call_id)
    spoken = texts(voice)
    assert GOODBYE not in spoken[spoken.index(EMERGENCY) :]
    assert STILL_THERE not in spoken[spoken.index(EMERGENCY) :]


async def test_an_emergency_during_the_greeting_is_not_heard(runtime: AsyncEngine) -> None:
    # The caller is not heard during the greeting, whatever they say: an emergency included.
    model = ScriptedModel()
    time = FakeTime()
    processor, playback = agent_call(new_call_id(), deps_on(runtime), model, time)
    voice = FakeVoice(autoplay=False)

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the greeting")
        await voice.start(2)
        await call.send(*caller_says(HEART_ATTACK))
        await voice.finish()
        await settle()
        assert texts(voice) == [GREETING]
        assert not call.ended
    await drain_call_events()


async def test_the_maximum_waits_for_the_running_turn(
    runtime: AsyncEngine, owner_engine: Engine
) -> None:
    # The call's maximum comes while the model works on a turn. The turn runs to its end; it
    # reports an emergency, whose line is said, and the call ends for it, with no goodbye.
    let_go, slow = held(calls(("report_emergency", {})))
    model = ScriptedModel(slow)
    time = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(call_id, deps_on(runtime), model, time)
    voice = FakeVoice(autoplay=False)

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the opening line")
        await voice.play_all()
        await call.send(*caller_says("My chest feels tight and my arm is numb."))
        await until(lambda: len(model.requests) == 1, "the model to be asked")
        await time.advance(600)
        assert voice.waiting == 0
        let_go.set()
        await ends_with_the_emergency(call, voice, owner_engine, call_id)
    assert GOODBYE not in texts(voice)

    # A turn that answers: its answer, then the goodbye, and the call ends at its maximum.
    let_go, slow = held(says("Your appointment is on Thursday."))
    model = ScriptedModel(slow)
    time = FakeTime()
    call_id = new_call_id()
    processor, playback = agent_call(call_id, deps_on(runtime), model, time)
    voice = FakeVoice()
    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the greeting")
        await call.send(*caller_says("When is my appointment?"))
        await until(lambda: len(model.requests) == 1, "the model to be asked")
        await time.advance(600)
        let_go.set()
        await until(lambda: call.ended, "the call to end")
    assert texts(voice)[-2:] == ["Your appointment is on Thursday.", GOODBYE]
    await drain_call_events()
    assert call_events(owner_engine, call_id)[-1] == ("call_ended", "max_duration")
