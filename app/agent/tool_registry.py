"""The tools the model may use, and the one way they run.

A tool is declared once (`Tool`) and registered on a `Registry`, which refuses a declaration
that breaks a rule (`check_declaration`). `tools_for` is the gate: the tools a call's state
allows right now. `dispatch` is the only caller of a handler: it re-checks the gate, validates
the arguments, then runs the handler, applies the transition the handler asks for and appends
the audit event in one database transaction, and commits. A refusal or a failure gives the
model fixed text and is audited in a transaction of its own.

The pipeline runs one tool call at a time per call: dispatch takes the state and returns the
next one, and two dispatches for the same call at once are not supported.

Logs carry event names, tool names that are registered, call ids, counts and exception class
names; never arguments, results or exception text.
"""

import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.agent.state import CallState, Move, Phase, Verify, apply, count_tool_error
from app.audit.log import AGENT, TOOL_NAME, Action, AuditLog, Event, Outcome, ResourceType

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

# At this many failed tool calls in one call, every outcome asks for a hand-off.
MAX_TOOL_ERRORS = 2

REFUSED_MESSAGE = "That is not available right now."
FAILED_MESSAGE = "That could not be done right now."


class ToolResult(BaseModel):
    """What a handler returns: data for the model, the transition it asks for, and the id of
    the record it acted on, for the audit event (of the tool's declared resource type)."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    data: dict[str, Any] = {}
    transition: Move | Verify | None = None
    resource_id: str | None = None


# A handler takes the session of dispatch's transaction, the call's state and the validated
# arguments. It must not commit; the patient it acts for is `state.patient_id`.
Handler = Callable[[AsyncSession, CallState, Any], Awaitable[ToolResult]]


class Tool(BaseModel):
    """A tool, declared once. Every field must be given."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", arbitrary_types_allowed=True
    )

    name: str
    arguments: type[BaseModel]
    handler: Handler
    phi: bool
    phases: frozenset[Phase] = Field(min_length=1)
    office_open_only: bool
    writes: bool
    audit_action: Action
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
    """What the model is told. A refusal or a failure is fixed text and no data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Status
    message: str | None
    data: dict[str, Any]
    hand_off: bool


class DeclarationError(ValueError):
    """A tool declaration that breaks a rule."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Deps:
    """What dispatch needs to run a tool: sessions as the runtime role, the audit log and the
    clock (aware datetimes)."""

    sessions: async_sessionmaker[AsyncSession]
    audit: AuditLog
    now: Callable[[], datetime]


def _key(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "")


_OWNER_KEYS = frozenset(_key(name) for name in OWNER_FIELDS)


def _schema_names(schema: object) -> set[str]:
    """Every property name anywhere in a JSON schema, nested models included."""
    found: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            found |= set(properties)
        for value in schema.values():
            found |= _schema_names(value)
    elif isinstance(schema, list):
        for value in schema:
            found |= _schema_names(value)
    return found


def check_declaration(tool: Tool) -> Tool:
    """The declaration, validated again, if it keeps every rule; DeclarationError otherwise."""
    try:
        declared = Tool.model_validate(dict(tool))
    except ValidationError as error:
        fields = sorted({str(detail["loc"][0]) for detail in error.errors() if detail["loc"]})
        raise DeclarationError(f"a tool must declare valid {', '.join(fields)}") from None
    if not TOOL_NAME.fullmatch(declared.name):
        raise DeclarationError("the tool name must be 2 to 40 lower-case letters and underscores")

    arguments = declared.arguments
    if arguments.model_config.get("extra") != "forbid":
        raise DeclarationError("the argument model must forbid extra fields")
    names = set(arguments.model_fields) | _schema_names(arguments.model_json_schema())
    owners = sorted(name for name in names if _key(name) in _OWNER_KEYS)
    if owners:
        raise DeclarationError(f"argument {owners[0]} names a patient or a record owner")

    if declared.phi and not declared.phases <= PHI_PHASES:
        raise DeclarationError("a phi tool may list only the serving and booking phases")

    if declared.writes:
        confirmed = arguments.model_fields.get("confirmed")
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


def _available(tool: Tool, state: CallState) -> bool:
    """The gate."""
    if state.phase in NO_TOOL_PHASES or state.phase not in tool.phases:
        return False
    if tool.office_open_only and not state.office_open:
        return False
    # A phi tool's phases are serving or booking only: registration refuses any other.
    if tool.phi and not (state.verified and state.office_open):
        return False
    return True


def _outcome(status: Status, state: CallState, data: dict[str, Any] | None = None) -> ToolOutcome:
    message = {
        Status.DONE: None,
        Status.REFUSED: REFUSED_MESSAGE,
        Status.FAILED: FAILED_MESSAGE,
    }[status]
    return ToolOutcome(
        status=status,
        message=message,
        data={} if data is None else data,
        hand_off=state.tool_errors >= MAX_TOOL_ERRORS,
    )


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Every registered tool, in registration order."""
        return tuple(self._tools.values())

    def register(self, tool: Tool) -> None:
        declared = check_declaration(tool)
        if declared.name in self._tools:
            raise DeclarationError("a tool of that name is already registered")
        self._schemas[declared.name] = declared.arguments.model_json_schema()
        self._tools[declared.name] = declared

    def tools_for(self, state: CallState) -> list[ToolDescription]:
        """The tools the state allows right now, in registration order."""
        return [
            ToolDescription(name=name, parameters=copy.deepcopy(self._schemas[name]))
            for name, tool in self._tools.items()
            if _available(tool, state)
        ]

    async def dispatch(
        self, state: CallState, name: str, raw_args: object, deps: Deps
    ) -> tuple[CallState, ToolOutcome]:
        """Run one tool call for the model and return the next state and what to tell it."""
        tool = self._tools.get(name)
        if tool is None:
            return await self._refuse(state, None, "unknown_tool", deps)
        if name not in {description.name for description in self.tools_for(state)}:
            return await self._refuse(state, name, "not_available", deps)
        try:
            arguments = tool.arguments.model_validate(raw_args)
        except Exception as error:
            count = error.error_count() if isinstance(error, ValidationError) else 0
            logger.warning("tool_arguments_refused tool=%s errors=%d", name, count)
            return await self._refuse(state, name, "bad_arguments", deps)

        try:
            async with deps.sessions() as session:
                committing = False

                def only_dispatch_commits(_session: Session) -> None:
                    if not committing:
                        raise RuntimeError("a tool handler may not commit")

                event.listen(session.sync_session, "before_commit", only_dispatch_commits)
                result = await tool.handler(session, state, arguments)
                if not isinstance(result, ToolResult):
                    raise TypeError("a handler returns a ToolResult")
                after = state if result.transition is None else apply(state, result.transition)
                await deps.audit.append(
                    session,
                    Event(
                        actor=AGENT,
                        action=tool.audit_action,
                        resource_type=tool.resource_type,
                        outcome=Outcome.SUCCESS,
                        call_id=state.call_id,
                        resource_id=result.resource_id,
                    ),
                    now=deps.now(),
                )
                committing = True
                await session.commit()
        except Exception as error:
            failed = count_tool_error(state)
            logger.warning(
                "tool_failed tool=%s call_id=%s error=%s tool_errors=%d",
                name,
                state.call_id,
                type(error).__name__,
                failed.tool_errors,
            )
            await self._record(failed, Action.TOOL_FAILED, Outcome.FAILURE, name, deps)
            return failed, _outcome(Status.FAILED, failed)
        logger.info("tool_called tool=%s call_id=%s", name, state.call_id)
        return after, _outcome(Status.DONE, after, result.data)

    async def _refuse(
        self, state: CallState, name: str | None, reason: str, deps: Deps
    ) -> tuple[CallState, ToolOutcome]:
        logger.warning(
            "tool_refused tool=%s reason=%s call_id=%s", name or "-", reason, state.call_id
        )
        await self._record(state, Action.TOOL_REFUSED, Outcome.REFUSED, name, deps)
        return state, _outcome(Status.REFUSED, state)

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


# The application's tools.
REGISTRY = Registry()
