"""The tools the agent has, and the one way they run.

A tool is declared once (`Tool`) and registered on a `Registry`, which refuses a declaration
that breaks a rule (`check_declaration`). The application's registry is filled and sealed by
one entry point (app/agent/tools.py). `tools_for` is the gate as the model sees it: the tools
it may call in a call's state right now. `dispatch` is the only caller of a handler: it checks
who is calling, re-checks the gate, bounds and validates the arguments, then runs the handler,
applies the transition the handler asks for and appends the audit event in one database
transaction, and commits, all within a deadline. A refusal or a failure gives fixed text and is
audited in a transaction of its own.

A tool is callable by the model, or by code only. A dispatch without a grant is the model's.
Code calls a code-only tool with the `Grant` registered with that tool: an object the code
holds, which nothing the model sends can be. Every gate rule applies to both.

The gate offers a tool in its declared phases, to its declared callers (a verified patient, a
new patient's call with or without the details, or nobody known yet), with the office open if
it says so, and a patient-data tool only to the verified caller. No argument may name whose
record a tool acts on, and none may be a date of birth, but for the one tool of each kind in
DOB_RULES: the identity claim, and a new patient's details. Each kind has its fixed phase and
callers, and is the only tool that may ask for its kind's transition.

The handler's transaction is checked before its event is appended: its id is read before the
handler runs and again after (`pg_current_xact_id`, unique for the life of the database
cluster), so a handler that ended it, through SQLAlchemy or with SQL text, fails the call. A
commit through SQLAlchemy is refused before it reaches the database. When the call is
cancelled mid-tool (the caller hangs up), the work is rolled back and no event is written for
that tool call; the call's own end event records the hang-up.

The pipeline runs one tool call at a time per call: dispatch takes the state and returns the
next one, and two dispatches for the same call at once are not supported.

Logs carry event names, tool names that are registered, call ids, counts and exception class
names; never arguments, results or exception text.
"""

import asyncio
import collections.abc
import copy
import json
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, get_args, get_origin

from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, ValidationError
from pydantic.fields import FieldInfo
from sqlalchemy import Connection, Text, cast, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.claims import ClaimKeys
from app.agent.scripts import ScriptId
from app.agent.state import (
    Caller,
    CallState,
    EndClaim,
    FailedAttempt,
    IdentityClaim,
    Move,
    NewPatientDetails,
    Phase,
    SlotsOffered,
    Verify,
    apply,
    caller_of,
    count_refusal,
    count_tool_error,
)
from app.audit.log import AGENT, TOOL_NAME, Action, AuditLog, Event, Outcome, ResourceType
from app.domain.clinic import CLINIC_TZ

logger = logging.getLogger(__name__)

# Tools that touch patient data run only for a verified caller, with the office open, here.
PHI_PHASES = frozenset({Phase.SERVING, Phase.BOOKING})
# No tool at all runs in these phases.
NO_TOOL_PHASES = frozenset(
    {Phase.GREETING, Phase.EMERGENCY, Phase.HANDOFF, Phase.CLOSING, Phase.ENDED}
)

# Argument names that would let the model say whose record to act on. Compared without case,
# underscores or hyphens. The record is always the verified caller's, from the state.
OWNER_FIELDS = frozenset(
    {
        "patient",
        "patient_id",
        "patient_name",
        "ssn",
        "ssn_last4",
        "social_security_number",
        "dob",
        "date_of_birth",
        "owner",
        "owner_id",
        "record_owner",
    }
)
# The one of them a tool declared to take a date of birth may have, as its own top-level field.
DOB_FIELDS = frozenset({"dob"})


class DobKind(StrEnum):
    """Why a tool may take a date of birth. Each kind is declared by one tool at most."""

    # The date of birth the caller claims, with their name: code matches the claim, and no
    # record is looked up by it.
    IDENTITY_CLAIM = "identity_claim"
    # A first-time caller's own, for the record their booking creates.
    NEW_PATIENT_DETAILS = "new_patient_details"


@dataclass(frozen=True, slots=True, kw_only=True)
class DobRule:
    """Where a tool of a kind runs, for whom, and the one transition that kind of tool alone
    may ask for (the details it took, for the state). Besides, such a tool is no patient-data
    tool, runs only with the office open, is callable by the model and may not verify."""

    description: str
    phases: frozenset[Phase]
    callers: frozenset[Caller]
    transition: type[IdentityClaim] | type[NewPatientDetails]


DOB_RULES: Mapping[DobKind, DobRule] = MappingProxyType(
    {
        DobKind.IDENTITY_CLAIM: DobRule(
            description="an identity claim",
            phases=frozenset({Phase.INTAKE}),
            callers=frozenset({Caller.UNIDENTIFIED}),
            transition=IdentityClaim,
        ),
        DobKind.NEW_PATIENT_DETAILS: DobRule(
            description="a new patient's details",
            phases=frozenset({Phase.BOOKING}),
            callers=frozenset({Caller.NEW_PATIENT, Caller.NEW_PATIENT_WITH_DETAILS}),
            transition=NewPatientDetails,
        ),
    }
)

# The actions dispatch and the call itself write; no tool may declare one as its own.
RESERVED_ACTIONS = frozenset(
    {Action.TOOL_REFUSED, Action.TOOL_FAILED, Action.CALL_STARTED, Action.CALL_ENDED}
)

# At this many failed tool calls in one call, or this many refused ones, every outcome asks
# for a hand-off.
MAX_TOOL_ERRORS = 2
MAX_TOOL_REFUSALS = 5

# A tool call's handler, its transaction and its audit event end within this many seconds, or
# the call fails.
DISPATCH_DEADLINE_SECONDS = 10
# Arguments larger than this, serialized as JSON, are refused before they are validated.
MAX_ARGUMENT_BYTES = 16 * 1024

REFUSED_MESSAGE = "That is not available right now."
FAILED_MESSAGE = "That could not be done right now."


class ToolResult(BaseModel):
    """What a handler returns: data for its caller, the transition it asks for, a line for code
    to speak, the action of its event when the tool declares more than one, and, for a tool
    whose resource type is neither `tool` nor `patient`, the id of the record it acted on."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    data: dict[str, Any] = {}
    transition: (
        Move
        | Verify
        | IdentityClaim
        | FailedAttempt
        | EndClaim
        | NewPatientDetails
        | SlotsOffered
        | None
    ) = None
    script: ScriptId | None = None
    audit_action: Action | None = None
    resource_id: str | None = None


class NoArguments(BaseModel):
    """The arguments of a tool that takes none."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True, kw_only=True)
class HandlerContext:
    """What a handler has besides its session: the instant of this tool call on the call's
    clock, and the hasher of identity claims."""

    now: datetime
    claim_keys: ClaimKeys


# A handler takes the session of dispatch's transaction, the call's state, the validated
# arguments and its context. It must not end the transaction; the patient it acts for is
# `state.patient_id`.
Handler = Callable[[AsyncSession, CallState, Any, HandlerContext], Awaitable[ToolResult]]


class Tool(BaseModel):
    """A tool, declared once. Every field must be given."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", arbitrary_types_allowed=True
    )

    name: str
    arguments: type[BaseModel]
    handler: Handler
    # False for a tool only code calls, with the grant registered with it.
    model_callable: bool
    phi: bool
    phases: frozenset[Phase] = Field(min_length=1)
    office_open_only: bool
    writes: bool
    # The one tool that may ask to verify the caller (a Verify transition).
    may_verify: bool
    # For the one tool of each kind (DOB_RULES), the kind of date of birth it takes.
    takes_date_of_birth: DobKind | None
    # Who it is for (state.caller_of), whatever the phase.
    callers: frozenset[Caller] = Field(min_length=1)
    # The action of the tool's events, with the outcome success, unless the handler names one
    # of outcome_actions, each with its fixed outcome.
    audit_action: Action
    outcome_actions: dict[Action, Outcome]
    resource_type: ResourceType


class ToolDescription(BaseModel):
    """A tool as the model is offered it: its name and its arguments' JSON schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    parameters: dict[str, Any]


class Status(StrEnum):
    DONE = "done"
    REFUSED = "refused"
    FAILED = "failed"


class ToolOutcome(BaseModel):
    """What the caller of dispatch is told. A refusal or a failure is fixed text and no data.
    A script is a line for code to speak."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Status
    message: str | None
    data: dict[str, Any]
    hand_off: bool
    script: ScriptId | None = None


class Grant:
    """Code's right to call one code-only tool: registered with the tool, held by the code
    that calls it and passed to dispatch."""

    __slots__ = ()


class DeclarationError(ValueError):
    """A tool declaration that breaks a rule."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Deps:
    """What dispatch needs to run a tool: sessions as the runtime role, the audit log, the
    clock (aware datetimes) and the hasher of identity claims."""

    sessions: async_sessionmaker[AsyncSession]
    audit: AuditLog
    now: Callable[[], datetime]
    claim_keys: ClaimKeys


def _key(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "")


_OWNER_KEYS = frozenset(_key(name) for name in OWNER_FIELDS)
_FREE_KEY_MAPPINGS = (
    dict,
    collections.abc.Mapping,
    collections.abc.MutableMapping,
)


def _types_in(annotation: object) -> Iterator[object]:
    """The annotation and every type inside it, recursively (list[X], X | None, ...)."""
    yield annotation
    for argument in get_args(annotation):
        yield from _types_in(argument)


def _models_in(model: type[BaseModel]) -> list[type[BaseModel]]:
    """The model and every model its fields hold, however deep, each once."""
    found = [model]
    for current in found:
        for field in current.model_fields.values():
            for inner in _types_in(field.annotation):
                if isinstance(inner, type) and issubclass(inner, BaseModel) and inner not in found:
                    found.append(inner)
    return found


def _names_of(name: str, field: FieldInfo) -> set[str]:
    """Every key the model could send for a field: its name, its alias and every validation
    alias (each choice, each step of each path)."""
    names = {name}
    if field.alias is not None:
        names.add(field.alias)
    aliases = field.validation_alias
    choices = aliases.choices if isinstance(aliases, AliasChoices) else [aliases]
    for choice in choices:
        if isinstance(choice, str):
            names.add(choice)
        elif isinstance(choice, AliasPath):
            names |= {step for step in choice.path if isinstance(step, str)}
    return names


def _takes_anything(annotation: object) -> bool:
    """A free-key mapping, Any or object, anywhere in the annotation."""
    for inner in _types_in(annotation):
        if inner is Any or inner is object:
            return True
        if inner in _FREE_KEY_MAPPINGS or get_origin(inner) in _FREE_KEY_MAPPINGS:
            return True
    return False


def _check_arguments(arguments: type[BaseModel], *, takes_dob: bool) -> None:
    """The argument rules. For a tool declared to take a date of birth, its own top-level
    fields may be named as in DOB_FIELDS."""
    for model in _models_in(arguments):
        if model.model_config.get("extra") != "forbid":
            raise DeclarationError(
                "every argument model, nested ones too, must forbid extra fields"
            )
        allowed = DOB_FIELDS if takes_dob and model is arguments else frozenset()
        for name, field in model.model_fields.items():
            owners = sorted(
                key
                for key in _names_of(name, field)
                if _key(key) in _OWNER_KEYS and key not in allowed
            )
            if owners:
                raise DeclarationError(f"argument {owners[0]} names a patient or a record owner")
            if _takes_anything(field.annotation):
                raise DeclarationError(f"argument {name} takes free keys or any value")


def check_declaration(tool: Tool) -> Tool:
    """The declaration, validated again, if it keeps every rule; DeclarationError otherwise."""
    try:
        declared = Tool.model_validate(dict(tool))
    except ValidationError as error:
        fields = sorted({str(detail["loc"][0]) for detail in error.errors() if detail["loc"]})
        raise DeclarationError(f"a tool must declare valid {', '.join(fields)}") from None
    if not TOOL_NAME.fullmatch(declared.name):
        raise DeclarationError("the tool name must be 2 to 40 lower-case letters and underscores")
    if {declared.audit_action, *declared.outcome_actions} & RESERVED_ACTIONS:
        raise DeclarationError("the audit action is reserved for dispatch and the call")
    if declared.audit_action in declared.outcome_actions:
        raise DeclarationError("the default action is not listed again in outcome_actions")

    kind = declared.takes_date_of_birth
    if kind is not None:
        rule = DOB_RULES[kind]
        if (
            declared.phi
            or declared.phases != rule.phases
            or declared.callers != rule.callers
            or not declared.office_open_only
            or not declared.model_callable
            or declared.may_verify
        ):
            raise DeclarationError(
                f"a tool that takes {rule.description} is not a phi tool, is in"
                f" {', '.join(sorted(rule.phases))} only, for"
                f" {', '.join(sorted(rule.callers))} callers, with the office open, is callable"
                " by the model and may not verify"
            )

    _check_arguments(declared.arguments, takes_dob=kind is not None)

    if declared.phi and not declared.phases <= PHI_PHASES:
        raise DeclarationError("a phi tool may list only the serving and booking phases")

    if declared.may_verify and (
        declared.phi or declared.phases != {Phase.INTAKE} or declared.model_callable
    ):
        raise DeclarationError(
            "a tool that may verify is not a phi tool, is in intake only and is code-only"
        )

    # The caller confirms what the model asks to write. Code calls a code-only tool on its own
    # decision, never on the model's word, and gives it nothing to write but the state.
    if declared.writes and not declared.model_callable and declared.arguments.model_fields:
        raise DeclarationError("a code-only writing tool takes no arguments")
    if declared.writes and declared.model_callable:
        confirmed = declared.arguments.model_fields.get("confirmed")
        if (
            confirmed is None
            or confirmed.annotation is not bool
            or confirmed.is_required()
            or confirmed.default is not False
            or confirmed.alias not in (None, "confirmed")
        ):
            raise DeclarationError(
                "a writing tool's arguments need confirmed: bool, defaulting to false"
            )
    return declared


def is_available(tool: Tool, state: CallState) -> bool:
    """The gate, for the model's calls and code's alike."""
    if state.phase in NO_TOOL_PHASES or state.phase not in tool.phases:
        return False
    if caller_of(state) not in tool.callers:
        return False
    if tool.office_open_only and not state.office_open:
        return False
    # A phi tool's phases are serving or booking only: registration refuses any other.
    if tool.phi and not (state.verified and state.patient_id is not None and state.office_open):
        return False
    return True


def _small_enough(raw_args: object) -> bool:
    """Whether the arguments serialize as JSON within MAX_ARGUMENT_BYTES."""
    try:
        serialized = json.dumps(raw_args)
    except (TypeError, ValueError):
        return False
    return len(serialized.encode("utf-8")) <= MAX_ARGUMENT_BYTES


def _resource_id(tool: Tool, state: CallState, result: ToolResult) -> str | None:
    """The id the tool call's event records: the tool's name, the caller's patient id, or for
    any other record the handler's."""
    if tool.resource_type in (ResourceType.TOOL, ResourceType.PATIENT):
        if result.resource_id is not None:
            raise ValueError("a handler does not name the tool or the patient")
        return tool.name if tool.resource_type is ResourceType.TOOL else state.patient_id
    return result.resource_id


def _event(tool: Tool, result: ToolResult) -> tuple[Action, Outcome]:
    """The action and outcome of a tool call's event: only ones the tool declares."""
    if result.audit_action is None or result.audit_action is tool.audit_action:
        return tool.audit_action, Outcome.SUCCESS
    if result.audit_action in tool.outcome_actions:
        return result.audit_action, tool.outcome_actions[result.audit_action]
    raise PermissionError("a handler names only an action its tool declares")


def _outcome(
    status: Status,
    state: CallState,
    data: dict[str, Any] | None = None,
    script: ScriptId | None = None,
) -> ToolOutcome:
    message = {
        Status.DONE: None,
        Status.REFUSED: REFUSED_MESSAGE,
        Status.FAILED: FAILED_MESSAGE,
    }[status]
    return ToolOutcome(
        status=status,
        message=message,
        data={} if data is None else data,
        hand_off=(state.tool_errors >= MAX_TOOL_ERRORS or state.tool_refusals >= MAX_TOOL_REFUSALS),
        script=script,
    )


# The id of the current transaction, assigned if it has none yet. Read before the handler and
# after: the same id only if the transaction is still the one the handler was given; one that
# ended was replaced by a new transaction with a new id.
_TRANSACTION_ID = select(cast(func.pg_current_xact_id(), Text))


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self._grants: dict[str, Grant] = {}
        self._sealed = False

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Every registered tool, in registration order."""
        return tuple(self._tools.values())

    @property
    def sealed(self) -> bool:
        return self._sealed

    def seal(self) -> None:
        """No tool can be registered after this."""
        self._sealed = True

    def register(self, tool: Tool, *, grant: Grant | None = None) -> None:
        """Register a tool: a code-only tool with a grant of its own, a model-callable tool
        without one."""
        if self._sealed:
            raise DeclarationError("the registry is sealed")
        declared = check_declaration(tool)
        if declared.model_callable != (grant is None) or (
            grant is not None and not isinstance(grant, Grant)
        ):
            raise DeclarationError(
                "a code-only tool is registered with a grant, a model-callable tool without one"
            )
        if grant is not None and any(given is grant for given in self._grants.values()):
            raise DeclarationError("a grant is registered with one tool only")
        if declared.name in self._tools:
            raise DeclarationError("a tool of that name is already registered")
        if declared.may_verify and any(item.may_verify for item in self._tools.values()):
            raise DeclarationError("only one tool may verify the caller")
        kind = declared.takes_date_of_birth
        if kind is not None and any(
            item.takes_date_of_birth is kind for item in self._tools.values()
        ):
            raise DeclarationError(f"only one tool may take {DOB_RULES[kind].description}")
        self._schemas[declared.name] = declared.arguments.model_json_schema()
        self._tools[declared.name] = declared
        if grant is not None:
            self._grants[declared.name] = grant

    def tools_for(self, state: CallState) -> list[ToolDescription]:
        """The tools the model may call in the state right now, in registration order."""
        return [
            ToolDescription(name=name, parameters=copy.deepcopy(self._schemas[name]))
            for name, tool in self._tools.items()
            if tool.model_callable and is_available(tool, state)
        ]

    async def dispatch(
        self,
        state: CallState,
        name: object,
        raw_args: object,
        deps: Deps,
        *,
        grant: Grant | None = None,
    ) -> tuple[CallState, ToolOutcome]:
        """Run one tool call and return the next state and what to tell the caller: the model
        when no grant is given, otherwise the code that holds the tool's grant."""
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return await self._refuse(state, None, "unknown_tool", deps)
        if grant is None and not tool.model_callable:
            return await self._refuse(state, tool.name, "code_only", deps)
        if grant is not None and self._grants.get(tool.name) is not grant:
            return await self._refuse(state, tool.name, "not_granted", deps)
        if not is_available(tool, state):
            return await self._refuse(state, tool.name, "not_available", deps)
        if not _small_enough(raw_args):
            return await self._refuse(state, tool.name, "too_large", deps)
        now = deps.now()
        try:
            # Argument models may check dates against the clinic's date, from the call's clock.
            arguments = tool.arguments.model_validate(
                raw_args, context={"today": now.astimezone(CLINIC_TZ).date()}
            )
        except Exception as error:
            count = error.error_count() if isinstance(error, ValidationError) else 0
            logger.warning("tool_arguments_refused tool=%s errors=%d", tool.name, count)
            return await self._refuse(state, tool.name, "bad_arguments", deps)

        try:
            async with asyncio.timeout(DISPATCH_DEADLINE_SECONDS), deps.sessions() as session:
                try:
                    connection = await session.connection()
                    committing = False

                    def only_dispatch_commits(_connection: Connection) -> None:
                        if not committing:
                            raise RuntimeError("a tool handler may not commit")

                    event.listen(connection.sync_connection, "commit", only_dispatch_commits)
                    opened = await session.scalar(_TRANSACTION_ID)
                    context = HandlerContext(now=now, claim_keys=deps.claim_keys)
                    result = await tool.handler(session, state, arguments, context)
                    if not isinstance(result, ToolResult):
                        raise TypeError("a handler returns a ToolResult")
                    if isinstance(result.transition, Verify) and not tool.may_verify:
                        raise PermissionError("only the tool declared to verify may verify")
                    for kind, rule in DOB_RULES.items():
                        if (
                            isinstance(result.transition, rule.transition)
                            and tool.takes_date_of_birth is not kind
                        ):
                            raise PermissionError("only the tool declared to take it may")
                    action, outcome = _event(tool, result)
                    after = state if result.transition is None else apply(state, result.transition)
                    resource_id = _resource_id(tool, after, result)
                    if await session.scalar(_TRANSACTION_ID) != opened:
                        raise RuntimeError("the handler ended the transaction it was given")
                    await deps.audit.append(
                        session,
                        Event(
                            actor=AGENT,
                            action=action,
                            resource_type=tool.resource_type,
                            outcome=outcome,
                            call_id=state.call_id,
                            resource_id=resource_id,
                        ),
                        now=deps.now(),
                    )
                    committing = True
                    await session.commit()
                except BaseException:
                    # Whatever state the handler left the connection in (a refused commit
                    # leaves the driver inside its transaction), it is closed, not reused: the
                    # server rolls back what it holds.
                    await session.invalidate()
                    raise
        except Exception as error:
            failed = count_tool_error(state)
            logger.warning(
                "tool_failed tool=%s call_id=%s error=%s tool_errors=%d",
                tool.name,
                state.call_id,
                type(error).__name__,
                failed.tool_errors,
            )
            await self._record(failed, Action.TOOL_FAILED, Outcome.FAILURE, tool.name, deps)
            return failed, _outcome(Status.FAILED, failed)
        logger.info("tool_called tool=%s call_id=%s", tool.name, state.call_id)
        return after, _outcome(Status.DONE, after, result.data, result.script)

    async def _refuse(
        self, state: CallState, name: str | None, reason: str, deps: Deps
    ) -> tuple[CallState, ToolOutcome]:
        refused = count_refusal(state)
        logger.warning(
            "tool_refused tool=%s reason=%s call_id=%s tool_refusals=%d",
            name or "-",
            reason,
            state.call_id,
            refused.tool_refusals,
        )
        await self._record(refused, Action.TOOL_REFUSED, Outcome.REFUSED, name, deps)
        return refused, _outcome(Status.REFUSED, refused)

    async def _record(
        self, state: CallState, action: Action, outcome: Outcome, name: str | None, deps: Deps
    ) -> None:
        """A refusal or failure's event, in a transaction of its own. If it cannot be written
        the outcome stands; the failure is logged."""
        try:
            async with deps.sessions() as session:
                await deps.audit.append(
                    session,
                    Event(
                        actor=AGENT,
                        action=action,
                        resource_type=ResourceType.TOOL,
                        outcome=outcome,
                        call_id=state.call_id,
                        resource_id=name,
                    ),
                    now=deps.now(),
                )
                await session.commit()
        except Exception as error:
            logger.error(
                "tool_audit_failed action=%s call_id=%s error=%s",
                action.value,
                state.call_id,
                type(error).__name__,
            )


# The application's tools: filled and sealed by app/agent/tools.py.
REGISTRY = Registry()
