"""Every tool the agent has, registered in one place.

`register_tools` is the one entry point: it registers each tool on the application's registry
and seals it, after which nothing more can be registered. The application calls it at start;
the structural tests call it too, so every tool registered here is bound by the gate's rules.

`submit_identity` is code-only: its grant stays in this module, and `submit_claim` is the one
way code calls it.
"""

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
)
from app.agent.verification import VerifyIdentityArguments, submit_identity, verify_identity
from app.audit.log import Action, Outcome, ResourceType

_SUBMIT_GRANT = Grant()

VERIFY_IDENTITY = Tool(
    name="verify_identity",
    arguments=VerifyIdentityArguments,
    handler=verify_identity,
    model_callable=True,
    phi=False,
    phases=frozenset({Phase.INTAKE}),
    office_open_only=True,
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
    office_open_only=True,
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


def register_tools() -> Registry:
    """Register every tool and seal the registry. Calling it again returns the sealed one."""
    if not REGISTRY.sealed:
        REGISTRY.register(VERIFY_IDENTITY)
        REGISTRY.register(SUBMIT_IDENTITY, grant=_SUBMIT_GRANT)
        for tool in SERVING_TOOLS:
            REGISTRY.register(tool)
        REGISTRY.seal()
    return REGISTRY


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
