"""The scenario runner: `python -m evals [--model scripted|real] [--runs N]`.

Each run of a scenario gets a database of its own (evals/database.py), seeded for the
scenario's day, and is one call as the live pipeline makes it (app/voice/pipeline.py): a call
id from the call registry, `start_call` on the application's tools (which writes call_started),
then each caller turn as text through `CallAgent.handle`, which runs the input guard, the gate,
dispatch and the scripted lines; nothing of them is copied here. The call then ends with
`CallAgent.end` (call_ended): for the reason its phase gives (emergency, transfer, goodbye),
otherwise as a caller who hangs up after the last turn. The call's clock is the scenario's
`now`; the audit and claim keys are random for the run.

The model is wrapped so that every request it is sent is kept: that is what the model was
shown. In scripted mode each turn's replies come from the scenario; in real mode they come from
the model of the settings and the scenario's replies are ignored. Real mode is run by hand
only: it refuses under pytest or in CI, and without an API key.

After each turn, and at the end of the call, the scenario's expectations are checked
(evals/scenario.py); in scripted mode a turn's scripted-only ones too. Each unmet one is a
Failure, named by where it is and its kind. Audit actions are read once the call's events and
the output guard's guard_block events still being written are. A turn's
flags are read from the input guard itself, given the turn and the state the turn starts in:
the guard is pure, so that is the decision the turn gets. The call's stored record is read once
the records still being made are. In scripted mode a turn must ask the
model exactly as many times as it has replies.

The report gives, for each scenario, the runs that passed and the first unmet expectation of
the first failed run, by name; never what anyone said.
"""

import argparse
import asyncio
import copy
import json
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.call_record import drain_call_records
from app.agent.claims import ClaimKeys
from app.agent.endings import drain_call_events
from app.agent.model import AnthropicModel, Model, ModelReply, ModelRequest, anthropic_model
from app.agent.scripts import PLACEHOLDER, SCRIPTS, ScriptId
from app.agent.state import Phase
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import (
    EMERGENCY_TURN,
    SWALLOWED_TURN,
    TurnResult,
    drain_guard_audits,
    start_call,
)
from app.audit.log import AuditLog, Reason
from app.config import Settings
from app.db.models import AuditEvent, CallSession
from app.db.session import create_session_factory
from app.domain.clinic import clinic_date
from app.guard.input import Flag, guard_input
from app.guard.logging import configure_logging
from app.guard.mask import spoken_digits
from app.guard.output import OutputRule
from app.runtime_config import taking_calls
from app.voice.calls import CallRegistry
from evals.database import EvalServer, eval_server, eval_settings
from evals.scenario import (
    TABLES,
    CallStep,
    Expect,
    RecordExpect,
    RowExpect,
    Scenario,
    ScenarioError,
    ToolExpect,
    Turn,
    TurnExpect,
    Withheld,
    load_scenario,
    scenario_files,
)
from evals.scripted import ScriptedModel, Step, calls, says

# Each scripted line as a pattern that its spoken form matches whole, whatever its values.
_LINES = {
    script: re.compile(
        "".join(
            "(.+)" if index % 2 else re.escape(piece)
            for index, piece in enumerate(PLACEHOLDER.split(line.text))
        )
    )
    for script, line in SCRIPTS.items()
}

# The line of the stored transcript each withheld turn leaves.
_WITHHELD_LINES = {
    f"caller: {SWALLOWED_TURN}": Withheld.SWALLOWED,
    f"caller: {EMERGENCY_TURN}": Withheld.EMERGENCY,
}

# The call registry only gives a run its call id: it never runs the call, so it never applies
# this limit on a call's length, which it needs to be built.
CALL_SECONDS = 600

Rows = dict[str, dict[tuple[Any, ...], dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Failure:
    """An unmet expectation: where (`turn N`, `call`, or `run` for a run that stopped), its
    kind, and what was found, in ids, names and counts."""

    scope: str
    kind: str
    detail: str

    @property
    def name(self) -> str:
        return f"{self.scope} {self.kind}"


class RealModeRefusedError(RuntimeError):
    """Real mode cannot run here."""


def real_model(load_settings: Callable[[], Settings], environ: Mapping[str, str]) -> AnthropicModel:
    """The model of the settings, for a run by hand. Under pytest or in CI it refuses before
    the settings are read; without an API key it refuses."""
    if "PYTEST_CURRENT_TEST" in environ or "CI" in environ:
        raise RealModeRefusedError("real mode is run by hand only, never under pytest or in CI")
    model = anthropic_model(load_settings())
    if model is None:
        raise RealModeRefusedError("real mode needs ANTHROPIC_API_KEY")
    return model


class _Recorded:
    """The model, with every request it was sent and its reply (None when it gave none)."""

    def __init__(self, model: Model) -> None:
        self._model = model
        self.requests: list[ModelRequest] = []
        self.replies: list[ModelReply | None] = []

    async def reply(self, request: ModelRequest, first_token: asyncio.Event) -> ModelReply:
        self.requests.append(copy.deepcopy(request))
        try:
            answer = await self._model.reply(request, first_token)
        except BaseException:
            self.replies.append(None)
            raise
        self.replies.append(answer)
        return answer


@dataclass(frozen=True, slots=True)
class _Called:
    """A tool the model asked for, and what it was told came of it, if it was told."""

    name: str
    arguments: object
    result: object


@dataclass(frozen=True, slots=True)
class _Seen:
    """What a turn, or the whole call, gave."""

    lines: list[str]
    requests: list[ModelRequest]
    called: list[_Called]
    actions: list[str]
    phase: Phase
    verified: bool


def _script_of(line: str) -> ScriptId | None:
    return next((script for script, form in _LINES.items() if form.fullmatch(line)), None)


def _texts(value: object) -> list[str]:
    """Every piece of text in a request: the prompt, the messages and the tools offered."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return []


def _shown(requests: list[ModelRequest]) -> list[str]:
    return [
        text.casefold()
        for request in requests
        for text in _texts([request.system, request.messages, request.tools])
    ]


def _forms(text: str) -> list[str]:
    """The text, and, when it is digit words alone, the digits it says."""
    forms = [text.casefold()]
    if all(spoken_digits(word) for word in text.split()):
        forms.append(spoken_digits(text))
    return forms


def _same(found: object, expected: object) -> bool:
    """Equal as JSON: true is not 1."""
    return json.dumps(found, sort_keys=True) == json.dumps(expected, sort_keys=True)


def _results(recorded: _Recorded, index: int) -> list[object] | None:
    """What the model was told came of the tools of reply `index`: the message after it, in the
    next request, if there is one."""
    reply = recorded.replies[index]
    if reply is None or index + 1 >= len(recorded.requests):
        return None
    messages = recorded.requests[index + 1].messages
    at = len(recorded.requests[index].messages) + 1
    if at >= len(messages) or not isinstance(messages[at]["content"], list):
        return None
    blocks = [block for block in messages[at]["content"] if block.get("type") == "tool_result"]
    if [block["tool_use_id"] for block in blocks] != [call.id for call in reply.tool_calls]:
        return None
    results: list[object] = [json.loads(block["content"]) for block in blocks]
    return results


def _called(recorded: _Recorded, first: int, last: int) -> list[_Called]:
    """The tools asked for in replies `first` up to `last`, in order."""
    called = []
    for index in range(first, last):
        reply = recorded.replies[index]
        if reply is None:
            continue
        results = _results(recorded, index)
        for position, call in enumerate(reply.tool_calls):
            result = None if results is None else results[position]
            called.append(_Called(call.name, call.arguments, result))
    return called


def _tool_failure(position: int, called: _Called, expected: ToolExpect) -> str | None:
    if called.name != expected.call:
        return f"call {position} is {called.name}, {expected.call} expected"
    arguments = called.arguments if isinstance(called.arguments, dict) else {}
    for key, value in expected.args.items():
        if key not in arguments or not _same(arguments[key], value):
            return f"call {position} argument {key} differs"
    if expected.returns is not None:
        data = called.result.get("data") if isinstance(called.result, dict) else None
        if not isinstance(data, dict):
            return f"call {position} returned no data"
        for key, value in expected.returns.items():
            if key not in data or not _same(data[key], value):
                return f"call {position} returned {key} differently"
    return None


async def _written(
    sessions: async_sessionmaker[AsyncSession], before: Rows
) -> dict[str, list[dict[str, Any]]]:
    """The rows added since `before`, or changed from what they were then, in each table."""
    return {
        table: [row for key, row in rows.items() if before[table].get(key) != row]
        for table, rows in (await _rows(sessions)).items()
    }


def _row_failure(
    index: int, written: dict[str, list[dict[str, Any]]], row: RowExpect
) -> str | None:
    found = sum(
        1
        for written_row in written[row.table]
        if all(_same(written_row[name], value) for name, value in row.where.items())
    )
    if found != row.count:
        return f"rows {index + 1} in {row.table}: {found} written, {row.count} expected"
    return None


def _never_failure(text: str, lines: list[str], shown: list[str]) -> str | None:
    forms = _forms(text)
    said = any(form in line.casefold() for form in forms for line in lines)
    seen = any(form in piece for form in forms for piece in shown)
    if said and seen:
        return f"{text!r} was said and shown to the model"
    if said:
        return f"{text!r} was said"
    if seen:
        return f"{text!r} was shown to the model"
    return None


async def _record_failures(
    sessions: async_sessionmaker[AsyncSession],
    call_id: str,
    reason: Reason,
    expect: RecordExpect,
) -> list[Failure]:
    """The call's stored record against `expect`, once the records still being made are:
    it exists, ends for `reason`, holds the withheld turns' markers in order and none of its
    `never` strings."""
    await drain_call_records()
    async with sessions() as session:
        statement = select(CallSession.end_reason, CallSession.transcript).where(
            CallSession.call_id == call_id
        )
        row = (await session.execute(statement)).first()
    if row is None:
        return [Failure("call", "record", "no record was stored")]
    end_reason, transcript = row
    found = []
    if end_reason != reason.value:
        found.append(f"the record ends for {end_reason}")
    withheld = [_WITHHELD_LINES[line] for line in transcript.split("\n") if line in _WITHHELD_LINES]
    if withheld != expect.withheld:
        found.append(f"{[kind.value for kind in withheld]} withheld")
    stored = transcript.casefold()
    found += [
        f"{text!r} was stored"
        for text in expect.never
        if any(form in stored for form in _forms(text))
    ]
    return [Failure("call", "record", detail) for detail in found]


def _offered(request: ModelRequest) -> set[str]:
    return {tool["name"] for tool in request.tools}


def _check(
    scope: str, expect: Expect, seen: _Seen, written: dict[str, list[dict[str, Any]]]
) -> list[Failure]:
    """The expectations a turn and the call share, in the order they are declared."""
    found: list[tuple[str, str]] = []
    if expect.said is not None:
        said = [script for script in map(_script_of, seen.lines) if script is not None]
        if said != expect.said:
            found.append(("said", f"{[s.value for s in said]} said"))
    if expect.tools is not None:
        if len(seen.called) != len(expect.tools):
            found.append(("tools", f"{len(seen.called)} calls, {len(expect.tools)} expected"))
        else:
            tool_failures = [
                failure
                for position, (called, tool) in enumerate(
                    zip(seen.called, expect.tools, strict=True), 1
                )
                if (failure := _tool_failure(position, called, tool)) is not None
            ]
            if tool_failures:
                found.append(("tools", tool_failures[0]))
    if expect.phase is not None and seen.phase is not expect.phase:
        found.append(("phase", f"in {seen.phase.value}"))
    if expect.verified is not None and seen.verified is not expect.verified:
        found.append(("verified", f"verified is {seen.verified}"))
    if expect.rows is not None:
        row_failures = [
            failure
            for index, row in enumerate(expect.rows)
            if (failure := _row_failure(index, written, row)) is not None
        ]
        if row_failures:
            found.append(("rows", row_failures[0]))
    if expect.audit is not None and seen.actions != [action.value for action in expect.audit]:
        found.append(("audit", f"{seen.actions} written"))
    if expect.never is not None:
        shown = _shown(seen.requests)
        found += [
            ("never", failure)
            for text in expect.never
            if (failure := _never_failure(text, seen.lines, shown)) is not None
        ]
    if expect.offered is not None:
        offered = [_offered(request) for request in seen.requests]
        if not offered:
            found.append(("offered", "the model was not asked"))
        elif any(tools != set(expect.offered) for tools in offered):
            found.append(("offered", f"{sorted(set().union(*offered))} offered"))
    if expect.never_offered is not None:
        offered = [_offered(request) for request in seen.requests]
        if not offered:
            found.append(("never_offered", "the model was not asked"))
        elif unwanted := set().union(*offered) & set(expect.never_offered):
            found.append(("never_offered", f"{sorted(unwanted)} offered"))
    return [Failure(scope, kind, detail) for kind, detail in found]


def _turn_failures(
    scope: str, expect: TurnExpect, flags: frozenset[Flag], result: TurnResult
) -> list[Failure]:
    """The expectations only a turn states."""
    found: list[tuple[str, str]] = []
    if expect.flags is not None and flags != set(expect.flags):
        found.append(("flags", f"{sorted(flag.value for flag in flags)} raised"))
    reached = result.shown is not None
    if expect.reaches_model is not None and reached != expect.reaches_model:
        found.append(("reaches_model", f"the model was {'' if reached else 'not '}shown the turn"))
    if expect.withheld is not None and result.withheld is not expect.withheld:
        found.append(("withheld", f"{_rule(result.withheld)} withheld"))
    return [Failure(scope, kind, detail) for kind, detail in found]


def _rule(rule: OutputRule | None) -> str:
    return "nothing" if rule is None else rule.value


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> Rows:
    """Every row of the tables a call writes, by primary key."""
    rows: Rows = {}
    async with sessions() as session:
        for name, table in TABLES.items():
            result = await session.execute(select(table))
            key = [column.name for column in table.primary_key.columns]
            rows[name] = {
                tuple(row[column] for column in key): dict(row) for row in result.mappings()
            }
    return rows


async def _actions(sessions: async_sessionmaker[AsyncSession], call_id: str) -> list[str]:
    """The call's audit actions, in order, once its events still being written are."""
    await drain_call_events()
    await drain_guard_audits()
    async with sessions() as session:
        return list(
            await session.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.call_id == call_id)
                .order_by(AuditEvent.seq)
            )
        )


def _steps(turn: Turn) -> list[Step]:
    """The turn's scripted replies, as the model gives them."""
    return [
        calls((step.call, step.args)) if isinstance(step, CallStep) else says(step.say)
        for step in turn.model
    ]


async def run_scenario(
    scenario: Scenario, server: EvalServer, model: Model | None = None
) -> list[Failure]:
    """One call of the scenario, on a database of its own: its unmet expectations. Without a
    model, the scenario's scripted replies answer."""
    scripted: ScriptedModel | None = None
    if model is None:
        scripted = ScriptedModel()
        recorded = _Recorded(scripted)
    else:
        recorded = _Recorded(model)
    async with server.database(clinic_date(scenario.now)) as engine:
        sessions = create_session_factory(engine)
        deps = Deps(
            sessions=sessions,
            audit=AuditLog(SecretStr(secrets.token_urlsafe(32))),
            now=lambda: scenario.now,
            claim_keys=ClaimKeys(SecretStr(secrets.token_urlsafe(32))),
            config=taking_calls(),
        )
        call_id = CallRegistry(max_calls=1, max_seconds=CALL_SECONDS).reserve()
        if call_id is None:
            raise RuntimeError("the call registry gave no call id")
        before = await _rows(sessions)
        agent, opening = await start_call(call_id, deps, recorded, register_tools())
        failures: list[Failure] = []
        lines = [opening.text]
        unseen: list[tuple[str, str]] = []
        try:
            for number, turn in enumerate(scenario.turns, 1):
                scope = f"turn {number}"
                if scripted is not None:
                    scripted.steps.clear()
                    scripted.then(*_steps(turn))
                flags = guard_input(turn.caller, agent.state).flags
                asked, logged = len(recorded.requests), len(await _actions(sessions, call_id))
                result = await agent.handle(turn.caller)
                lines += result.lines
                times = len(recorded.requests) - asked
                if scripted is not None and times != len(turn.model):
                    detail = f"asked {times} times, {len(turn.model)} replies scripted"
                    failures.append(Failure(scope, "model_script", detail))
                expects = [turn.expect]
                if scripted is not None:
                    expects.append(turn.expect_scripted)
                stated = [expect for expect in expects if expect is not None]
                if not stated:
                    continue
                if any(expect.reaches_model is False for expect in stated):
                    unseen.append((scope, turn.caller))
                seen = _Seen(
                    lines=list(result.lines),
                    requests=recorded.requests[asked:],
                    called=_called(recorded, asked, len(recorded.replies)),
                    actions=(await _actions(sessions, call_id))[logged:],
                    phase=agent.state.phase,
                    verified=agent.state.verified,
                )
                written = await _written(sessions, before)
                for expect in stated:
                    failures += _check(scope, expect, seen, written)
                    failures += _turn_failures(scope, expect, flags, result)
            # The call's last phase, then its end as the live pipeline ends it: for the reason
            # that phase gives, or, when the scenario's turns run out first, because the caller
            # hangs up.
            phase = agent.state.phase
            reason = agent.ending or Reason.CALLER_HUNG_UP
            await agent.end(reason)
            seen = _Seen(
                lines=lines,
                requests=recorded.requests,
                called=_called(recorded, 0, len(recorded.replies)),
                actions=await _actions(sessions, call_id),
                phase=phase,
                verified=agent.state.verified,
            )
            failures += _check("call", scenario.expect, seen, await _written(sessions, before))
            if scenario.expect.record is not None:
                failures += await _record_failures(
                    sessions, call_id, reason, scenario.expect.record
                )
            shown = _shown(recorded.requests)
            failures += [
                Failure(scope, "reaches_model", "the turn's words reached the model later")
                for scope, caller in unseen
                if any(caller.casefold() in piece for piece in shown)
            ]
        finally:
            await drain_call_events()
            await drain_guard_audits()
            await drain_call_records()
    return failures


async def run_all(
    paths: Sequence[Path], server: EvalServer, model: Model | None, runs: int
) -> dict[str, list[list[Failure]]]:
    """Each scenario `runs` times: by scenario name, each run's failures. A run that stops
    with an error fails, with the error's class."""
    results: dict[str, list[list[Failure]]] = {}
    for path in paths:
        scenario = load_scenario(path)
        outcomes = []
        for _run in range(runs):
            try:
                outcomes.append(await run_scenario(scenario, server, model))
            except Exception as error:
                outcomes.append([Failure("run", "error", type(error).__name__)])
        results[path.stem] = outcomes
    return results


def report(results: Mapping[str, list[list[Failure]]]) -> tuple[str, bool]:
    """The table of results, and whether any run failed."""
    width = max(len("scenario"), *(len(name) for name in results))
    lines = [f"{'scenario':<{width}}  passed  first unmet expectation"]
    for name, outcomes in results.items():
        passed = f"{sum(1 for failures in outcomes if not failures)}/{len(outcomes)}"
        first = next(
            (
                f"run {run}: {failures[0].name}"
                for run, failures in enumerate(outcomes, 1)
                if failures
            ),
            "",
        )
        lines.append(f"{name:<{width}}  {passed:<6}  {first}".rstrip())
    failed = any(failures for outcomes in results.values() for failures in outcomes)
    return "\n".join(lines) + "\n", failed


async def _evaluate(model: AnthropicModel | None, runs: int) -> dict[str, list[list[Failure]]]:
    try:
        settings = eval_settings()
        configure_logging(settings)
        with eval_server(settings) as server:
            return await run_all(scenario_files(), server, model, runs)
    finally:
        if model is not None:
            await model.close()


def _runs(text: str) -> int:
    runs = int(text)
    if runs < 1:
        raise argparse.ArgumentTypeError("runs must be at least 1")
    return runs


def main(argv: Sequence[str] | None = None) -> int:
    """Run every scenario: 0 when every run passed, 1 when one failed, 2 when the runs could
    not start."""
    parser = argparse.ArgumentParser(
        prog="python -m evals", description="Run the demo calls as text through the turn loop."
    )
    parser.add_argument("--model", choices=("scripted", "real"), default="scripted")
    parser.add_argument("--runs", type=_runs, default=1)
    arguments = parser.parse_args(argv)
    try:
        model = real_model(Settings, os.environ) if arguments.model == "real" else None
    except (RealModeRefusedError, ValidationError) as error:
        sys.stderr.write(f"{error}\n")
        return 2
    try:
        # psycopg's async mode needs a selector event loop; Windows defaults to the proactor.
        results = asyncio.run(
            _evaluate(model, arguments.runs), loop_factory=asyncio.SelectorEventLoop
        )
    except (ValidationError, ScenarioError) as error:
        sys.stderr.write(f"{error}\n")
        return 2
    text, failed = report(results)
    sys.stdout.write(text)
    return 1 if failed else 0
