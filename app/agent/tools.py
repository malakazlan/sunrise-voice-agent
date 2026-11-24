"""Every tool the agent has, registered in one place.

`register_tools` is the one entry point: it registers each tool on the application's registry
and seals it, after which nothing more can be registered. The application calls it at start;
the structural tests call it too, so every tool registered here is bound by the gate's rules.

`submit_identity` is code-only: its grant stays in this module, and `submit_claim` is the one
way code calls it. So is `hand_off_to_staff`, which `call_tool` runs when an outcome asks for a
hand-off.
"""

from app.agent.endings import (
    ENDING_TOOLS,
    REPORT_EMERGENCY,
    TAKE_MESSAGE,
    TOOL_PHASES,
    emergency,
    hand_off_to_staff,
)
from app.agent.scripts import ScriptId
from app.agent.serving import SERVING_TOOLS
from app.agent.state import CallState, Phase, drop_claim
from app.agent.tool_registry import (
    REGISTRY,
    Deps,
    Grant,
    NoArguments,
    Registry,
    Status,
    Tool,
    ToolOutcome,
    is_available,
)
from app.agent.verification import VerifyIdentityArguments, submit_identity, verify_identity
from app.audit.log import Action, Outcome, Reason, ResourceType

_SUBMIT_GRANT = Grant()
_HAND_OFF_GRANT = Grant()

VERIFY_IDENTITY = Tool(
    name="verify_identity",
    arguments=VerifyIdentityArguments,
    handler=verify_identity,
    model_callable=True,
    phi=False,
    phases=frozenset({Phase.INTAKE}),
    closed_phases=frozenset(),
    writes=False,
    may_verify=False,
    takes_identity_claim=True,
    audit_action=Action.IDENTITY_CLAIMED,
    outcome_actions={Action.IDENTITY_LOCKED: Outcome.REFUSED},
    resource_type=ResourceType.TOOL,
)

SUBMIT_IDENTITY = Tool(
    name="submit_identity",
    arguments=NoArguments,
    handler=submit_identity,
    model_callable=False,
    phi=False,
    phases=frozenset({Phase.INTAKE}),
    closed_phases=frozenset(),
    writes=True,
    may_verify=True,
    takes_identity_claim=False,
    audit_action=Action.IDENTITY_VERIFIED,
    outcome_actions={
        Action.IDENTITY_REFUSED: Outcome.REFUSED,
        Action.IDENTITY_LOCKED: Outcome.REFUSED,
    },
    resource_type=ResourceType.PATIENT,
)

HAND_OFF_TO_STAFF = Tool(
    name="hand_off_to_staff",
    arguments=NoArguments,
    handler=hand_off_to_staff,
    model_callable=False,
    phi=False,
    phases=TOOL_PHASES,
    closed_phases=frozenset(),
    writes=True,
    may_verify=False,
    takes_identity_claim=False,
    audit_action=Action.TRANSFER_REQUESTED,
    outcome_actions={},
    resource_type=ResourceType.CALL,
)


def register_tools() -> Registry:
    """Register every tool and seal the registry. Calling it again returns the sealed one."""
    if not REGISTRY.sealed:
        REGISTRY.register(VERIFY_IDENTITY)
        REGISTRY.register(SUBMIT_IDENTITY, grant=_SUBMIT_GRANT)
        for tool in (*SERVING_TOOLS, *ENDING_TOOLS):
            REGISTRY.register(tool)
        REGISTRY.register(HAND_OFF_TO_STAFF, grant=_HAND_OFF_GRANT)
        REGISTRY.seal()
    return REGISTRY


async def call_tool(
    state: CallState, name: object, raw_args: object, deps: Deps
) -> tuple[CallState, ToolOutcome]:
    """The model's tool call, and what code does about its outcome. A report of an emergency
    that dispatch could not carry out (the database is down) moves the call and gives the
    emergency line all the same. An outcome that asks for a hand-off moves a call to the staff
    while the office is open, and offers a closed office's caller the message instead; a call
    already on its way out is left to go."""
    after, outcome = await REGISTRY.dispatch(state, name, raw_args, deps)
    if name == REPORT_EMERGENCY.name and outcome.status is Status.FAILED:
        after, script = await emergency(after, Reason.MODEL, deps)
        return after, outcome.model_copy(update={"script": script})
    if not outcome.hand_off:
        return after, outcome
    if is_available(HAND_OFF_TO_STAFF, after):
        return await REGISTRY.dispatch(
            after, HAND_OFF_TO_STAFF.name, {}, deps, grant=_HAND_OFF_GRANT
        )
    if is_available(TAKE_MESSAGE, after):
        return after, outcome.model_copy(update={"script": ScriptId.GUARD_FALLBACK})
    return after, outcome


async def submit_claim(state: CallState, deps: Deps) -> tuple[CallState, ToolOutcome]:
    """Run submit_identity for the claim and the digits in the state, as the input guard asked.
    If it does not run to an outcome, the claim and the digits are dropped all the same, and
    nothing is counted or verified."""
    after, outcome = await REGISTRY.dispatch(
        state, SUBMIT_IDENTITY.name, {}, deps, grant=_SUBMIT_GRANT
    )
    if outcome.status is not Status.DONE:
        after = drop_claim(after)
    return after, outcome
