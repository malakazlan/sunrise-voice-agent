"""One call's conversation, a caller turn at a time. No Pipecat here: the voice pipeline
(app/voice/pipeline.py) hands this module each finished utterance as text, and speaks each line
this module releases.

`start_call` starts the call with `start_call` of app/agent/endings.py (the office hours decided
by code from the call's clock, its `call_started` event), moves it to intake and gives the
greeting or the closed line. `CallAgent.handle` then takes each turn through the input guard
(app/guard/input.py):

- emergency: `emergency` (app/agent/endings.py) moves the call to emergency and gives the
  emergency line at once, its event written in the background; the model is not asked;
- swallow: the model sees nothing of the turn; if the guard says submit, code runs
  submit_claim and its line is the reply;
- pass: a submit the guard asks for runs first; then one model turn with the guard's text.

A model turn sends the system prompt, the conversation so far and the tools `tools_for` allows
in the state right now. Every tool call goes through `call_tool` (app/agent/tools.py): a report
of an emergency takes the emergency's one way, whatever its arguments; any other call goes to
`dispatch`, and the outcome that reaches the limit of failures or refusals hands the call off.
The outcome goes back to the model, unless it carries a scripted line: that line, with its
values, is the reply, word for word, and the model is told it was said. A turn has at most
MAX_TOOL_ROUNDS tool rounds. A refusal, any failed request, no first token within
FIRST_TOKEN_SECONDS, a reply not finished within REPLY_SECONDS after it, a reply that is not a
plain answer or a tool request, one tool round too many, or a reply whose first sentence is
over the output guard's length bound: the fixed fallback line, and the call goes on.
MAX_MODEL_FAILURES of them in one call raise the hand-off signal, as does a tool outcome that
asks for one (`call_tool` has acted on that one already).

The hand-off acts once, on the failure or the block that raises it, by the code-only path a
failing tool's hand-off takes (`hand_off`, app/agent/tools.py): with the office open the call is
handed to the staff, whose transfer line is said in place of the turn's own, and the call, now
in handoff, ends; with it closed the caller is offered a message (the fallback line) and the
call goes on.

A plain answer goes through the output guard (app/guard/output.py) a sentence at a time before
it is returned: what passes is said; a blocked sentence is replaced by its rule's scripted line
and the rest of the reply is dropped. The blocked text goes nowhere: not into the conversation,
not to a log, not to the audit log, whose `guard_block` event has the rule's name as its reason.
That event is written in a task of its own after the reply, so the reply never waits on the
database; a failure to write it is logged and the block stands. `drain_guard_audits`, at
shutdown, waits for those writes a bounded time, and a write cut off is logged.
MAX_GUARD_BLOCKS blocked replies in a row raise the hand-off signal; a model reply said in full
starts the count again. A message a tool returned this turn for the caller to confirm is the
caller's own text: the guard does not take it for the model's advice or promise when the reply
reads it back.

Each line is released as soon as it is decided, to the `speak` callback the turn was given: a
scripted line at once, the model's guarded sentences, as one line, once its reply has ended as a
plain answer. The words of a reply that asks for a tool, is refused or is cut off are never
spoken. A tool round whose answer has not come FILLER_SECONDS after the model asked for the tools
gets the filler line, once per turn: only once the round's tools have answered with no scripted
line, and never while the SSN step is armed.

What is said joins the conversation as assistant messages, scripted lines included; swallowed
turns never join it. A line the caller did not hear in full (`heard`) keeps only the words
heard, or leaves the conversation. Turns are handled one at a time (a lock), each guard decision
applied to the state before the next turn is read. The conversation is kept in memory only, for
the call's life. `end` is the one way the call ends.

Beside the conversation, the agent keeps the history the call record stores (`history`): every
message the model is shown and every line said, trimmed by `heard` as the conversation is, with
a fixed marker in place of each turn the model was given nothing of (SWALLOWED_TURN,
EMERGENCY_TURN), and without the opening message code gives the model. It is never sent to the
model. Once `end` has ended the call it hands that history to `record_call`
(app/agent/call_record.py), which masks and stores it in a task of its own: the end does not
wait for it, and a failure to start it is logged, never raised.

Logs carry event names, the call id, rule names, counts and exception class names; never text.
"""

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from app.agent import endings
from app.agent.call_record import record_call
from app.agent.endings import emergency, end_call
from app.agent.model import Model, ModelReply, ModelRequest, ToolCall
from app.agent.prompts import SYSTEM_PROMPT, TOOL_DESCRIPTIONS
from app.agent.scripts import SCRIPTS, ScriptId, placeholders
from app.agent.serving import NEEDS_CONFIRMATION
from app.agent.state import CallState, Phase, apply, move
from app.agent.tool_registry import (
    REGISTRY,
    Deps,
    Registry,
    Status,
    ToolDescription,
    ToolOutcome,
)
from app.agent.tools import call_tool, hand_off, offers_a_message, submit_claim
from app.audit.log import AGENT, Action, Event, Outcome, Reason, ResourceType
from app.guard.input import GuardAction, guard_input
from app.guard.output import GuardedReply, OutputRule, guard_reply

logger = logging.getLogger(__name__)

FIRST_TOKEN_SECONDS = 8.0
REPLY_SECONDS = 20.0
MAX_TOOL_ROUNDS = 4
MAX_MODEL_FAILURES = 2
MAX_GUARD_BLOCKS = 2
# How long after the model asks for tools the filler line may be said, if nothing has been.
FILLER_SECONDS = 1.2
# The phases that end a call once their line has been said, and the reason each ends it for.
ENDINGS = MappingProxyType(
    {
        Phase.EMERGENCY: Reason.EMERGENCY,
        Phase.HANDOFF: Reason.TRANSFERRED,
        Phase.CLOSING: Reason.COMPLETED,
    }
)
# How long `drain_guard_audits` waits for guard_block events still being written.
AUDIT_WAIT_SECONDS = 10.0
# The caller's own words a tool returns for confirmation, to be read back: a message to the
# doctor (send_message_to_doctor) and the reason for a callback (take_message).
READ_BACK_FIELDS = ("message", "reason_short")
# The guard_block events still being written, every call's, held so that they run to the end.
_GUARD_AUDITS: set[asyncio.Task[None]] = set()

# The reason a guard_block event records for each rule of the output guard.
BLOCK_REASONS = {
    OutputRule.SSN_LAST4: Reason.SSN_LAST4,
    OutputRule.SSN_SHAPE: Reason.SSN_SHAPE,
    OutputRule.PHI_OUTSIDE_GATE: Reason.PHI_OUTSIDE_GATE,
    OutputRule.MEDICAL_ADVICE: Reason.MEDICAL_ADVICE,
    OutputRule.REFILL_PROMISE: Reason.REFILL_PROMISE,
}
# The Messages API takes a conversation that starts with the caller; this, fixed and written by
# code, is the caller's first message, before the opening line.
CALL_CONNECTED = "(The call has connected.)"
# What the call record's history holds in place of a turn the model was given nothing of.
SWALLOWED_TURN = "(turn withheld)"
EMERGENCY_TURN = "(emergency turn withheld)"

# What the model is told about a tool call that ended its turn with a scripted line, and about
# one asked for after it in the same reply, which does not run.
SAID_BY_THE_SYSTEM = {"status": "done", "said": "the system spoke the next line"}
NOT_RUN = {"status": "not_run", "said": "the turn had ended"}


@dataclass(frozen=True, slots=True)
class TurnTimings:
    """Milliseconds from the turn's start to the first and the last token the model sent,
    if it was asked, and the tool rounds it took."""

    first_token_ms: int | None
    last_token_ms: int | None
    tool_rounds: int


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What came of a turn: the guard's action, what the model was given (None when it was
    given nothing), the lines said in order, whether a hand-off is asked for, and the output
    guard's rule when it withheld part of the model's reply."""

    action: GuardAction
    shown: str | None = field(repr=False)
    lines: tuple[str, ...] = field(repr=False)
    hand_off: bool
    timings: TurnTimings
    withheld: OutputRule | None


@dataclass(frozen=True, slots=True)
class Line:
    """A line released to be spoken: its id in the call, its text, and its script, if code
    wrote it."""

    id: int
    text: str = field(repr=False)
    script: ScriptId | None

    @property
    def interruptible(self) -> bool:
        """Whether the caller may cut it short: every line but the scripts that say not."""
        return self.script is None or SCRIPTS[self.script].interruptible


# Called with each line as it is released, in order.
Speak = Callable[[Line], None]


class _ModelFailedError(Exception):
    """A model turn that ends with the fallback line. Its reason is an event name."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _offered(description: ToolDescription) -> dict[str, Any]:
    return {
        "name": description.name,
        "description": TOOL_DESCRIPTIONS[description.name],
        "input_schema": description.parameters,
    }


def _result(call: ToolCall, content: dict[str, Any], *, error: bool) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": call.id,
        "content": json.dumps(content),
        "is_error": error,
    }


class CallAgent:
    def __init__(
        self,
        state: CallState,
        deps: Deps,
        model: Model,
        registry: Registry,
        *,
        first_token_seconds: float = FIRST_TOKEN_SECONDS,
        reply_seconds: float = REPLY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # The tools are offered from the registry they run on: the application's, which
        # call_tool runs them through.
        if registry is not REGISTRY:
            raise ValueError("a call's tools are the application registry's")
        self._state = state
        self._deps = deps
        self._model = model
        self._registry = registry
        self._first_token_seconds = first_token_seconds
        self._reply_seconds = reply_seconds
        self._clock = clock
        self._sleep = sleep
        self._messages: list[dict[str, Any]] = [{"role": "user", "content": CALL_CONNECTED}]
        self._failures = 0
        self._blocks = 0
        self._hand_off = False
        self._lock = asyncio.Lock()
        self._line_ids = itertools.count(1)
        # Each line said and not yet heard in full, by id: its message in the conversation.
        self._unheard: dict[int, dict[str, Any]] = {}
        # Where the lines go as they are released, while a turn or a prompt runs.
        self._speak: Speak | None = None
        # The texts of the lines released in the turn running, in order.
        self._turn_lines: list[str] = []
        self._filler_said = False
        # The history the call record stores: what the model is shown and what is said, and
        # the marker of each turn the model never saw. Never sent to the model.
        self._record: list[dict[str, Any]] = []

    @property
    def state(self) -> CallState:
        return self._state

    @property
    def history(self) -> tuple[dict[str, Any], ...]:
        """The history the call record stores; never sent to the model."""
        return tuple(self._record)

    @property
    def ending(self) -> Reason | None:
        """The reason the call ends for once its last line is said, when its phase says it
        does: emergency, hand-off or closing."""
        return ENDINGS.get(self._state.phase)

    def say(self, script: ScriptId, values: Mapping[str, str] | None = None) -> str:
        """A scripted line, with exactly the values its placeholders name, added to the
        conversation as the model's own and released."""
        return self._say(script, values).text

    def _say(self, script: ScriptId, values: Mapping[str, str] | None = None) -> Line:
        given = {} if values is None else dict(values)
        if set(given) != placeholders(script):
            raise ValueError("a line is said with exactly the values it names")
        text = SCRIPTS[script].text.format(**given) if given else SCRIPTS[script].text
        return self._release(text, script)

    def _release(self, text: str, script: ScriptId | None) -> Line:
        """The line joins the conversation and goes to `speak`."""
        line = Line(id=next(self._line_ids), text=text, script=script)
        message = {"role": "assistant", "content": text}
        self._append(message)
        self._unheard[line.id] = message
        self._turn_lines.append(text)
        if self._speak is not None:
            self._speak(line)
        return line

    def heard(self, line_id: int, words: int | None = None) -> None:
        """What the caller heard of a line: all of it (`words` None), or its first `words`
        words, which stay in the conversation in its place; with none, it leaves it."""
        message = self._unheard.pop(line_id, None)
        if message is None or words is None:
            return
        said = " ".join(message["content"].split()[:words])
        for messages in (self._messages, self._record):
            at = next(index for index, kept in enumerate(messages) if kept is message)
            if said:
                messages[at] = {"role": "assistant", "content": said}
            else:
                del messages[at]

    def _append(self, message: dict[str, Any]) -> None:
        """A message joins the conversation and the call record's history."""
        self._messages.append(message)
        self._record.append(message)

    def _withheld(self, marker: str) -> None:
        """A turn the model is given nothing of leaves its marker in the record's history."""
        self._record.append({"role": "user", "content": marker})

    def _line_for(self, outcome: ToolOutcome) -> str:
        if outcome.script is not None:
            return self.say(outcome.script, outcome.script_values)
        if outcome.message is not None:
            return self._release(outcome.message, None).text
        return self.say(ScriptId.FALLBACK)

    async def handle(self, transcript: str, speak: Speak | None = None) -> TurnResult:
        """One caller turn; each line said goes to `speak` as soon as it is released."""
        async with self._lock:
            self._speak = speak
            try:
                return await self._handle(transcript)
            finally:
                self._speak = None

    async def prompt(self, script: ScriptId, speak: Speak) -> Line:
        """A scripted line code says between turns (the idle prompt, a goodbye)."""
        async with self._lock:
            self._speak = speak
            try:
                return self._say(script)
            finally:
                self._speak = None

    def is_emergency(self, transcript: str) -> bool:
        """Whether the input guard reads an emergency in what the caller said: its decision,
        the one owner of the emergency phrases."""
        return guard_input(transcript, self._state).action is GuardAction.EMERGENCY

    async def say_emergency(self, speak: Speak) -> Line:
        """The emergency's one way (`emergency`, app/agent/endings.py) for an emergency heard
        outside a turn: the call moves to emergency and its line is released at once. It takes
        no lock: the voice adapter calls it with the running turn cancelled, and nothing here
        waits, so a turn cannot run in between."""
        self._state, script = await emergency(self._state, Reason.GUARD, self._deps)
        self._withheld(EMERGENCY_TURN)
        self._speak = speak
        try:
            return self._say(script)
        finally:
            self._speak = None

    async def end(self, reason: Reason) -> None:
        """The one place a call ends: `end_call` (app/agent/endings.py) writes its call_ended
        event, once, with the reason; then the call's record is started, once, and not
        waited for."""
        ended_before = self._state.phase is Phase.ENDED
        self._state = await end_call(self._state, reason, self._deps)
        if ended_before:
            return
        try:
            record_call(self._state, self.history, reason, self._deps)
        except Exception as error:
            logger.error(
                "call_record_not_started call_id=%s error=%s",
                self._state.call_id,
                type(error).__name__,
            )

    async def _handle(self, transcript: str) -> TurnResult:
        started = self._clock()
        self._turn_lines = []
        self._filler_said = False
        decision = guard_input(transcript, self._state)
        timings = TurnTimings(first_token_ms=None, last_token_ms=None, tool_rounds=0)

        if decision.action is GuardAction.EMERGENCY:
            # The emergency's one way: the move and the line first, its event in the background.
            self._withheld(EMERGENCY_TURN)
            self._state, script = await emergency(self._state, Reason.GUARD, self._deps)
            self.say(script)
            return self._turn(decision.action, None, timings)

        if decision.action is GuardAction.SWALLOW:
            self._withheld(SWALLOWED_TURN)
        if decision.change is not None:
            self._state = apply(self._state, decision.change)

        if decision.submit:
            self._state, outcome = await submit_claim(self._state, self._deps)
            self._hand_off = self._hand_off or outcome.hand_off
            self._line_for(outcome)
        elif decision.script is not None:
            self.say(decision.script)

        if decision.action is GuardAction.SWALLOW or decision.text is None:
            return self._turn(decision.action, None, timings)

        withheld, timings = await self._model_turn(decision.text, started)
        return self._turn(decision.action, decision.text, timings, withheld)

    def _turn(
        self,
        action: GuardAction,
        shown: str | None,
        timings: TurnTimings,
        withheld: OutputRule | None = None,
    ) -> TurnResult:
        return TurnResult(
            action=action,
            shown=shown,
            lines=tuple(self._turn_lines),
            hand_off=self._hand_off,
            timings=timings,
            withheld=withheld,
        )

    async def _model_turn(self, text: str, started: float) -> tuple[OutputRule | None, TurnTimings]:
        """One caller turn the model answers, its lines released as they are decided; the
        output guard's rule if it blocked part of the answer."""
        self._append({"role": "user", "content": text})
        marks: list[float] = []
        rounds = 0
        withheld: OutputRule | None = None
        # The caller's own texts the tools returned this turn to be read back.
        read_back: list[str] = []
        # When the last tool round started, once its tools have answered with no line.
        round_started: float | None = None
        try:
            while True:
                reply = await self._ask(marks, round_started)
                if reply.stop_reason == "tool_use" and reply.tool_calls:
                    if rounds == MAX_TOOL_ROUNDS:
                        raise _ModelFailedError("tool_rounds")
                    rounds += 1
                    asked_at = self._clock()
                    self._append({"role": "assistant", "content": reply.content})
                    scripted = await self._run_tools(reply.tool_calls, read_back)
                    if scripted is not None:
                        self._line_for(scripted)
                        break
                    round_started = asked_at
                    continue
                if reply.stop_reason == "end_turn" and reply.text.strip() and not reply.tool_calls:
                    guarded = guard_reply([reply.text], self._state, read_back=tuple(read_back))
                    if not guarded.said and guarded.block is None:
                        raise _ModelFailedError("reply_too_long")
                    await self._say_guarded(guarded)
                    withheld = None if guarded.block is None else guarded.block.rule
                    break
                raise _ModelFailedError(
                    "refusal" if reply.stop_reason == "refusal" else "stop_reason"
                )
        except _ModelFailedError as failure:
            await self._fall_back(failure.reason)
        first = round((marks[0] - started) * 1000) if marks else None
        last = round((marks[-1] - started) * 1000) if marks else None
        timings = TurnTimings(first_token_ms=first, last_token_ms=last, tool_rounds=rounds)
        return withheld, timings

    async def _filler(self, round_started: float) -> None:
        """The filler line FILLER_SECONDS after the tool round started, unless the turn has
        said it already, the SSN step is armed or the call is ending."""
        await self._sleep(max(0.0, round_started + FILLER_SECONDS - self._clock()))
        if self._filler_said or self._state.expecting_ssn4 or self.ending is not None:
            return
        self._filler_said = True
        self.say(ScriptId.FILLER)

    async def _say_guarded(self, guarded: GuardedReply) -> None:
        """The sentences that passed, as one line, and the block's scripted line, each added to
        the conversation as it is released; when the block raises the hand-off, what the
        hand-off says instead. The blocked text is not kept."""
        if guarded.said:
            self._release(" ".join(guarded.said), None)
        if guarded.block is None:
            self._blocks = 0
            return
        self._blocks += 1
        raises = self._blocks >= MAX_GUARD_BLOCKS and not self._hand_off
        if self._blocks >= MAX_GUARD_BLOCKS:
            self._hand_off = True
        logger.warning(
            "guard_block call_id=%s rule=%s blocks=%d hand_off=%s",
            self._state.call_id,
            guarded.block.rule.value,
            self._blocks,
            self._hand_off,
        )
        self._audit_block(guarded.block.rule)
        if raises and await self._hands_off():
            return
        self.say(guarded.block.script)

    def _audit_block(self, rule: OutputRule) -> None:
        """The block's guard_block event, written in a task of its own so that the reply does
        not wait for it."""
        task = asyncio.create_task(self._write_block(self._state.call_id, rule, self._deps.now()))
        _GUARD_AUDITS.add(task)
        task.add_done_callback(_GUARD_AUDITS.discard)

    async def _write_block(self, call_id: str, rule: OutputRule, now: datetime) -> None:
        try:
            async with self._deps.sessions() as session:
                await self._deps.audit.append(
                    session,
                    Event(
                        actor=AGENT,
                        action=Action.GUARD_BLOCK,
                        resource_type=ResourceType.CALL,
                        outcome=Outcome.REFUSED,
                        call_id=call_id,
                        resource_id=call_id,
                        reason=BLOCK_REASONS[rule],
                    ),
                    now=now,
                )
                await session.commit()
        except asyncio.CancelledError:
            logger.error("guard_audit_cancelled call_id=%s rule=%s", call_id, rule.value)
            raise
        except Exception as error:
            logger.error(
                "guard_audit_failed call_id=%s rule=%s error=%s",
                call_id,
                rule.value,
                type(error).__name__,
            )

    async def _ask(self, marks: list[float], round_started: float | None) -> ModelReply:
        """One request with the tools allowed now. `marks` gets the instant of the first
        token and of the end of the reply. After a tool round (`round_started`), the filler
        may be said while the reply is awaited."""
        request = ModelRequest(
            system=SYSTEM_PROMPT,
            messages=list(self._messages),
            tools=[_offered(tool) for tool in self._registry.tools_for(self._state)],
        )
        first_token = asyncio.Event()
        replying = asyncio.ensure_future(self._model.reply(request, first_token))
        started = asyncio.ensure_future(first_token.wait())
        filler = (
            None if round_started is None else asyncio.ensure_future(self._filler(round_started))
        )
        try:
            done, _pending = await asyncio.wait(
                {replying, started},
                timeout=self._first_token_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise _ModelFailedError("first_token_timeout")
            if first_token.is_set() and not marks:
                marks.append(self._clock())
            try:
                reply = await asyncio.wait_for(replying, timeout=self._reply_seconds)
            except TimeoutError:
                raise _ModelFailedError("reply_timeout") from None
            except Exception as error:
                logger.warning(
                    "model_request_failed call_id=%s error=%s",
                    self._state.call_id,
                    type(error).__name__,
                )
                raise _ModelFailedError("request_failed") from None
            ended = self._clock()
            if not marks:
                marks.append(ended)
            marks.append(ended)
            return reply
        finally:
            started.cancel()
            if filler is not None:
                filler.cancel()
                await asyncio.gather(filler, return_exceptions=True)
            if not replying.done():
                replying.cancel()
                await asyncio.gather(replying, return_exceptions=True)

    async def _run_tools(
        self, tool_calls: list[ToolCall], read_back: list[str]
    ) -> ToolOutcome | None:
        """Each tool through `call_tool`, one at a time, with its result for the model. The
        first outcome that carries a scripted line ends the turn and is returned; tools asked
        for after it do not run. A message an outcome returns for the caller to confirm is
        added to `read_back`."""
        results: list[dict[str, Any]] = []
        scripted: ToolOutcome | None = None
        for call in tool_calls:
            if scripted is not None:
                results.append(_result(call, NOT_RUN, error=True))
                continue
            self._state, outcome = await call_tool(
                self._state, call.name, call.arguments, self._deps
            )
            self._hand_off = self._hand_off or outcome.hand_off
            if outcome.script is not None:
                scripted = outcome
                results.append(_result(call, SAID_BY_THE_SYSTEM, error=False))
                continue
            if outcome.data.get("status") == NEEDS_CONFIRMATION:
                read_back += [
                    text
                    for key in READ_BACK_FIELDS
                    if isinstance(text := outcome.data.get(key), str)
                ]
            content = {
                "status": outcome.status.value,
                "message": outcome.message,
                "data": outcome.data,
            }
            results.append(_result(call, content, error=outcome.status is not Status.DONE))
        self._append({"role": "user", "content": results})
        return scripted

    async def _fall_back(self, reason: str) -> None:
        """The fallback line; when this failure raises the hand-off, what the hand-off says
        instead."""
        self._failures += 1
        raises = self._failures >= MAX_MODEL_FAILURES and not self._hand_off
        if self._failures >= MAX_MODEL_FAILURES:
            self._hand_off = True
        logger.warning(
            "model_fallback call_id=%s reason=%s failures=%d hand_off=%s",
            self._state.call_id,
            reason,
            self._failures,
            self._hand_off,
        )
        if raises and await self._hands_off():
            return
        self.say(ScriptId.FALLBACK)

    async def _hands_off(self) -> bool:
        """The hand-off acting, once, on the outcome that raised it. With the office open the
        call goes to the staff the way a failing tool's does (`hand_off` of app/agent/tools.py):
        its line is said and the call is in handoff, which ends it. With it closed, the caller
        is offered a message instead: the fallback line. False when there is neither, and the
        turn's own line is said."""
        handed = await hand_off(self._state, self._deps)
        if handed is not None:
            self._state, outcome = handed
            self._line_for(outcome)
            return True
        if offers_a_message(self._state):
            self.say(ScriptId.FALLBACK)
            return True
        return False


async def drain_guard_audits(timeout: float = AUDIT_WAIT_SECONDS) -> None:
    """For shutdown and tests: wait up to `timeout` seconds for the guard_block events still
    being written, every call's; one still running then is cancelled, and logged as
    cancelled."""
    if not _GUARD_AUDITS:
        return
    _done, late = await asyncio.wait(set(_GUARD_AUDITS), timeout=timeout)
    for task in late:
        task.cancel()
    await asyncio.gather(*late, return_exceptions=True)


async def start_call(
    call_id: str,
    deps: Deps,
    model: Model,
    registry: Registry,
    *,
    first_token_seconds: float = FIRST_TOKEN_SECONDS,
    reply_seconds: float = REPLY_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[CallAgent, Line]:
    """A new call, started by `start_call` of app/agent/endings.py and moved to intake, and its
    opening line: the greeting while the office is open, otherwise the closed line."""
    state, opening = await endings.start_call(call_id, deps)
    agent = CallAgent(
        move(state, Phase.INTAKE),
        deps,
        model,
        registry,
        first_token_seconds=first_token_seconds,
        reply_seconds=reply_seconds,
        clock=clock,
        sleep=sleep,
    )
    return agent, agent._say(opening)
