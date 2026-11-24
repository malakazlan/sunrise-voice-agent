"""One call's conversation, a caller turn at a time. No Pipecat here: the voice pipeline
(app/voice/pipeline.py) hands this module each finished utterance as text.

`start_call` starts the call's state (the office hours decided by code from the call's clock),
says the greeting or the closed line and moves the call to intake. `CallAgent.handle` then
takes each turn through the input guard (app/guard/input.py):

- emergency: the emergency line, the call moves to emergency, and the model is not asked;
- swallow: the model sees nothing of the turn; if the guard says submit, code runs
  submit_claim and its line is the reply;
- pass: a submit the guard asks for runs first; then one model turn with the guard's text.

A model turn sends the system prompt, the conversation so far and the tools `tools_for` allows
in the state right now. Every tool call goes through `dispatch`, and its outcome goes back to
the model, unless it carries a scripted line: that line is the reply, word for word, and the
model is told it was said. A turn has at most MAX_TOOL_ROUNDS tool rounds. A refusal, any
failed request, no first token within FIRST_TOKEN_SECONDS, a reply not finished within
REPLY_SECONDS after it, a reply that is not a plain answer or a tool request, one tool round
too many, or a reply whose first sentence is over the output guard's length bound: the fixed
fallback line, and the call goes on. MAX_MODEL_FAILURES of them in one call raise the hand-off
signal, as does an outcome of dispatch that asks for one.

A plain answer goes through the output guard (app/guard/output.py) a sentence at a time before
it is returned: what passes is said; a blocked sentence is replaced by its rule's scripted line
and the rest of the reply is dropped. The blocked text goes nowhere: not into the conversation,
not to a log, not to the audit log, whose `guard_block` event has the rule's name as its reason.
That event is written in a task of its own after the reply, so the reply never waits on the
database; a failure to write it is logged and the block stands. MAX_GUARD_BLOCKS blocked
replies in a row raise the hand-off signal; a model reply said in full starts the count again.

What is said joins the conversation as assistant messages, scripted lines included; swallowed
turns never join it. Turns are handled one at a time (a lock), each guard decision applied to
the state before the next turn is read. The conversation is kept in memory only, for the call's
life.

Logs carry event names, the call id, rule names, counts and exception class names; never text.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agent.model import Model, ModelReply, ModelRequest, ToolCall
from app.agent.prompts import SYSTEM_PROMPT, TOOL_DESCRIPTIONS
from app.agent.scripts import SCRIPTS, ScriptId
from app.agent.state import CallState, Phase, apply, move, new_call
from app.agent.tool_registry import Deps, Registry, Status, ToolDescription, ToolOutcome
from app.agent.tools import submit_claim
from app.audit.log import AGENT, Action, Event, Outcome, Reason, ResourceType
from app.domain.office_hours import is_open
from app.guard.input import GuardAction, guard_input
from app.guard.output import GuardedReply, OutputRule, guard_reply

logger = logging.getLogger(__name__)

FIRST_TOKEN_SECONDS = 8.0
REPLY_SECONDS = 20.0
MAX_TOOL_ROUNDS = 4
MAX_MODEL_FAILURES = 2
MAX_GUARD_BLOCKS = 2

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
    ) -> None:
        self._state = state
        self._deps = deps
        self._model = model
        self._registry = registry
        self._first_token_seconds = first_token_seconds
        self._reply_seconds = reply_seconds
        self._clock = clock
        self._messages: list[dict[str, Any]] = [{"role": "user", "content": CALL_CONNECTED}]
        self._failures = 0
        self._blocks = 0
        self._hand_off = False
        self._lock = asyncio.Lock()
        # The guard_block events still being written, held so that they run to the end.
        self._audits: set[asyncio.Task[None]] = set()

    @property
    def state(self) -> CallState:
        return self._state

    def say(self, script: ScriptId) -> str:
        """A scripted line, added to the conversation as the model's own."""
        line = SCRIPTS[script].text
        if script is ScriptId.VERIFIED:
            if self._state.patient_first_name is None:
                raise ValueError("the verified line needs a verified caller")
            line = line.format(first_name=self._state.patient_first_name)
        self._messages.append({"role": "assistant", "content": line})
        return line

    def _line_for(self, outcome: ToolOutcome) -> str:
        if outcome.script is not None:
            return self.say(outcome.script)
        if outcome.message is not None:
            self._messages.append({"role": "assistant", "content": outcome.message})
            return outcome.message
        return self.say(ScriptId.FALLBACK)

    async def handle(self, transcript: str) -> TurnResult:
        async with self._lock:
            return await self._handle(transcript)

    async def settle(self) -> None:
        """Wait for the guard_block events still being written."""
        await asyncio.gather(*self._audits)

    async def _handle(self, transcript: str) -> TurnResult:
        started = self._clock()
        decision = guard_input(transcript, self._state)
        if decision.change is not None:
            self._state = apply(self._state, decision.change)
        lines: list[str] = []
        timings = TurnTimings(first_token_ms=None, last_token_ms=None, tool_rounds=0)

        if decision.action is GuardAction.EMERGENCY:
            lines.append(self.say(ScriptId.EMERGENCY))
            return self._turn(decision.action, None, lines, timings)

        if decision.submit:
            self._state, outcome = await submit_claim(self._state, self._deps)
            self._hand_off = self._hand_off or outcome.hand_off
            lines.append(self._line_for(outcome))
        elif decision.script is not None:
            lines.append(self.say(decision.script))

        if decision.action is GuardAction.SWALLOW or decision.text is None:
            return self._turn(decision.action, None, lines, timings)

        said, withheld, timings = await self._model_turn(decision.text, started)
        lines += said
        return self._turn(decision.action, decision.text, lines, timings, withheld)

    def _turn(
        self,
        action: GuardAction,
        shown: str | None,
        lines: list[str],
        timings: TurnTimings,
        withheld: OutputRule | None = None,
    ) -> TurnResult:
        return TurnResult(
            action=action,
            shown=shown,
            lines=tuple(lines),
            hand_off=self._hand_off,
            timings=timings,
            withheld=withheld,
        )

    async def _model_turn(
        self, text: str, started: float
    ) -> tuple[list[str], OutputRule | None, TurnTimings]:
        """The lines said for one caller turn the model answers, and the output guard's rule
        if it blocked part of the answer."""
        self._messages.append({"role": "user", "content": text})
        marks: list[float] = []
        rounds = 0
        withheld: OutputRule | None = None
        try:
            while True:
                reply = await self._ask(marks)
                if reply.stop_reason == "tool_use" and reply.tool_calls:
                    if rounds == MAX_TOOL_ROUNDS:
                        raise _ModelFailedError("tool_rounds")
                    rounds += 1
                    self._messages.append({"role": "assistant", "content": reply.content})
                    script = await self._run_tools(reply.tool_calls)
                    if script is not None:
                        said = [self.say(script)]
                        break
                    continue
                if reply.stop_reason == "end_turn" and reply.text.strip() and not reply.tool_calls:
                    guarded = guard_reply([reply.text], self._state)
                    if not guarded.said and guarded.block is None:
                        raise _ModelFailedError("reply_too_long")
                    said = self._say_guarded(guarded)
                    withheld = None if guarded.block is None else guarded.block.rule
                    break
                raise _ModelFailedError(
                    "refusal" if reply.stop_reason == "refusal" else "stop_reason"
                )
        except _ModelFailedError as failure:
            said = [self._fall_back(failure.reason)]
        first = round((marks[0] - started) * 1000) if marks else None
        last = round((marks[-1] - started) * 1000) if marks else None
        timings = TurnTimings(first_token_ms=first, last_token_ms=last, tool_rounds=rounds)
        return said, withheld, timings

    def _say_guarded(self, guarded: GuardedReply) -> list[str]:
        """The sentences that passed, as one line, and the block's scripted line, each added to
        the conversation as it is said. The blocked text is not kept."""
        said: list[str] = []
        if guarded.said:
            line = " ".join(guarded.said)
            self._messages.append({"role": "assistant", "content": line})
            said.append(line)
        if guarded.block is None:
            self._blocks = 0
            return said
        self._blocks += 1
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
        said.append(self.say(guarded.block.script))
        return said

    def _audit_block(self, rule: OutputRule) -> None:
        """The block's guard_block event, written in a task of its own so that the reply does
        not wait for it."""
        task = asyncio.create_task(self._write_block(self._state.call_id, rule, self._deps.now()))
        self._audits.add(task)
        task.add_done_callback(self._audits.discard)

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
        except Exception as error:
            logger.error(
                "guard_audit_failed call_id=%s rule=%s error=%s",
                call_id,
                rule.value,
                type(error).__name__,
            )

    async def _ask(self, marks: list[float]) -> ModelReply:
        """One request with the tools allowed now. `marks` gets the instant of the first
        token and of the end of the reply."""
        request = ModelRequest(
            system=SYSTEM_PROMPT,
            messages=list(self._messages),
            tools=[_offered(tool) for tool in self._registry.tools_for(self._state)],
        )
        first_token = asyncio.Event()
        replying = asyncio.ensure_future(self._model.reply(request, first_token))
        started = asyncio.ensure_future(first_token.wait())
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
            if not replying.done():
                replying.cancel()
                await asyncio.gather(replying, return_exceptions=True)

    async def _run_tools(self, tool_calls: list[ToolCall]) -> ScriptId | None:
        """Each tool through dispatch, one at a time, with its result for the model. The first
        outcome that carries a scripted line ends the turn; tools asked for after it do not
        run."""
        results: list[dict[str, Any]] = []
        script: ScriptId | None = None
        for call in tool_calls:
            if script is not None:
                results.append(_result(call, NOT_RUN, error=True))
                continue
            self._state, outcome = await self._registry.dispatch(
                self._state, call.name, call.arguments, self._deps
            )
            self._hand_off = self._hand_off or outcome.hand_off
            if outcome.script is not None:
                script = outcome.script
                results.append(_result(call, SAID_BY_THE_SYSTEM, error=False))
                continue
            content = {
                "status": outcome.status.value,
                "message": outcome.message,
                "data": outcome.data,
            }
            results.append(_result(call, content, error=outcome.status is not Status.DONE))
        self._messages.append({"role": "user", "content": results})
        return script

    def _fall_back(self, reason: str) -> str:
        self._failures += 1
        if self._failures >= MAX_MODEL_FAILURES:
            self._hand_off = True
        logger.warning(
            "model_fallback call_id=%s reason=%s failures=%d hand_off=%s",
            self._state.call_id,
            reason,
            self._failures,
            self._hand_off,
        )
        return self.say(ScriptId.FALLBACK)


def start_call(
    call_id: str,
    deps: Deps,
    model: Model,
    registry: Registry,
    *,
    first_token_seconds: float = FIRST_TOKEN_SECONDS,
    reply_seconds: float = REPLY_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[CallAgent, str]:
    """A new call in intake, and its opening line: the greeting while the office is open,
    otherwise the closed line."""
    now = deps.now()
    office_open = is_open(now)
    state = move(new_call(call_id=call_id, started_at=now, office_open=office_open), Phase.INTAKE)
    agent = CallAgent(
        state,
        deps,
        model,
        registry,
        first_token_seconds=first_token_seconds,
        reply_seconds=reply_seconds,
        clock=clock,
    )
    opening = agent.say(ScriptId.GREETING if office_open else ScriptId.CLOSED)
    return agent, opening
