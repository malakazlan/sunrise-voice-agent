"""One call's conversation, a caller turn at a time. No Pipecat here: the voice pipeline
(app/voice/pipeline.py) hands this module each finished utterance as text.

`start_call` starts the call's state (the office hours decided by code from the call's clock),
says the greeting or the closed line and moves the call to intake. `CallAgent.handle` then
takes each turn through the input guard (app/guard/input.py):

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
plain answer or a tool request, or one tool round too many: the fixed fallback line, and the
call goes on. MAX_MODEL_FAILURES of them in one call raise the hand-off signal, as does a tool
outcome that asks for one.

Scripted lines join the conversation as assistant messages; swallowed turns never join it.
Turns are handled one at a time (a lock), each guard decision applied to the state before the
next turn is read. The conversation is kept in memory only, for the call's life.

Logs carry event names, the call id, counts and exception class names; never text.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.agent.endings import emergency
from app.agent.model import Model, ModelReply, ModelRequest, ToolCall
from app.agent.prompts import SYSTEM_PROMPT, TOOL_DESCRIPTIONS
from app.agent.scripts import SCRIPTS, ScriptId, placeholders
from app.agent.state import CallState, Phase, apply, move, new_call
from app.agent.tool_registry import (
    REGISTRY,
    Deps,
    Registry,
    Status,
    ToolDescription,
    ToolOutcome,
)
from app.agent.tools import call_tool, submit_claim
from app.audit.log import Reason
from app.domain.office_hours import is_open
from app.guard.input import GuardAction, guard_input

logger = logging.getLogger(__name__)

FIRST_TOKEN_SECONDS = 8.0
REPLY_SECONDS = 20.0
MAX_TOOL_ROUNDS = 4
MAX_MODEL_FAILURES = 2
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
    given nothing), the lines said in order, and whether a hand-off is asked for."""

    action: GuardAction
    shown: str | None = field(repr=False)
    lines: tuple[str, ...] = field(repr=False)
    hand_off: bool
    timings: TurnTimings


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
        self._messages: list[dict[str, Any]] = [{"role": "user", "content": CALL_CONNECTED}]
        self._failures = 0
        self._hand_off = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CallState:
        return self._state

    def say(self, script: ScriptId, values: Mapping[str, str] | None = None) -> str:
        """A scripted line, with exactly the values its placeholders name, added to the
        conversation as the model's own."""
        given = {} if values is None else dict(values)
        if set(given) != placeholders(script):
            raise ValueError("a line is said with exactly the values it names")
        line = SCRIPTS[script].text.format(**given) if given else SCRIPTS[script].text
        self._messages.append({"role": "assistant", "content": line})
        return line

    def _line_for(self, outcome: ToolOutcome) -> str:
        if outcome.script is not None:
            return self.say(outcome.script, outcome.script_values)
        if outcome.message is not None:
            self._messages.append({"role": "assistant", "content": outcome.message})
            return outcome.message
        return self.say(ScriptId.FALLBACK)

    async def handle(self, transcript: str) -> TurnResult:
        async with self._lock:
            return await self._handle(transcript)

    async def _handle(self, transcript: str) -> TurnResult:
        started = self._clock()
        decision = guard_input(transcript, self._state)
        lines: list[str] = []
        timings = TurnTimings(first_token_ms=None, last_token_ms=None, tool_rounds=0)

        if decision.action is GuardAction.EMERGENCY:
            # The emergency's one way: the move and the line first, its event in the background.
            self._state, script = await emergency(self._state, Reason.GUARD, self._deps)
            lines.append(self.say(script))
            return self._turn(decision.action, None, lines, timings)

        if decision.change is not None:
            self._state = apply(self._state, decision.change)

        if decision.submit:
            self._state, outcome = await submit_claim(self._state, self._deps)
            self._hand_off = self._hand_off or outcome.hand_off
            lines.append(self._line_for(outcome))
        elif decision.script is not None:
            lines.append(self.say(decision.script))

        if decision.action is GuardAction.SWALLOW or decision.text is None:
            return self._turn(decision.action, None, lines, timings)

        reply, timings = await self._model_turn(decision.text, started)
        lines.append(reply)
        return self._turn(decision.action, decision.text, lines, timings)

    def _turn(
        self, action: GuardAction, shown: str | None, lines: list[str], timings: TurnTimings
    ) -> TurnResult:
        return TurnResult(
            action=action,
            shown=shown,
            lines=tuple(lines),
            hand_off=self._hand_off,
            timings=timings,
        )

    async def _model_turn(self, text: str, started: float) -> tuple[str, TurnTimings]:
        self._messages.append({"role": "user", "content": text})
        marks: list[float] = []
        rounds = 0
        try:
            while True:
                reply = await self._ask(marks)
                if reply.stop_reason == "tool_use" and reply.tool_calls:
                    if rounds == MAX_TOOL_ROUNDS:
                        raise _ModelFailedError("tool_rounds")
                    rounds += 1
                    self._messages.append({"role": "assistant", "content": reply.content})
                    scripted = await self._run_tools(reply.tool_calls)
                    if scripted is not None:
                        line = self._line_for(scripted)
                        break
                    continue
                if reply.stop_reason == "end_turn" and reply.text.strip() and not reply.tool_calls:
                    self._messages.append({"role": "assistant", "content": reply.content})
                    line = reply.text
                    break
                raise _ModelFailedError(
                    "refusal" if reply.stop_reason == "refusal" else "stop_reason"
                )
        except _ModelFailedError as failure:
            line = self._fall_back(failure.reason)
        first = round((marks[0] - started) * 1000) if marks else None
        last = round((marks[-1] - started) * 1000) if marks else None
        return line, TurnTimings(first_token_ms=first, last_token_ms=last, tool_rounds=rounds)

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

    async def _run_tools(self, tool_calls: list[ToolCall]) -> ToolOutcome | None:
        """Each tool through `call_tool`, one at a time, with its result for the model. The
        first outcome that carries a scripted line ends the turn and is returned; tools asked
        for after it do not run."""
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
            content = {
                "status": outcome.status.value,
                "message": outcome.message,
                "data": outcome.data,
            }
            results.append(_result(call, content, error=outcome.status is not Status.DONE))
        self._messages.append({"role": "user", "content": results})
        return scripted

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
