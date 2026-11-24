"""A call's turns in text: the input guard, the model with the tools the gate allows, the
scripted lines. The model is a scripted fake that records every request; nothing reaches the
network. Expected lines and tool results are written by hand from the scripts, the seed data
(app/db/seed_data.json) and the spec."""

import asyncio
import copy
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY, VOICE_SECRETS, offline_settings
from httpx2 import AsyncClient, MockTransport, Request, Response
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent.claims import ClaimKeys
from app.agent.model import (
    ModelReply,
    ModelRequest,
    ToolCall,
    anthropic_model,
)
from app.agent.prompts import SYSTEM_PROMPT, TOOL_DESCRIPTIONS
from app.agent.state import IdentityClaim, Phase, claim, move, new_call
from app.agent.tool_registry import Deps, Registry
from app.agent.tools import register_tools
from app.agent.turn import CALL_CONNECTED, CallAgent, start_call
from app.audit.log import AuditLog
from app.db.seed import load_seed_data, seed
from app.db.session import create_database_engine, create_session_factory
from app.guard.input import GuardAction
from app.voice.calls import CallRegistry

pytestmark = pytest.mark.anyio

# Tuesday 24 November 2026, 10:00 in clinic time: the office is open.
T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
# The same day at 18:00 in clinic time: closed.
EVENING = datetime(2026, 11, 24, 23, 0, tzinfo=UTC)
SEED_DAY = date(2026, 11, 24)

GREETING = (
    "Thank you for calling Sunrise Family Clinic. This call may be recorded for quality"
    " purposes. How can I help you today?"
)
CLOSED = (
    "Our office is currently closed. Our hours are Monday to Friday, 9 AM to 5 PM Eastern Time."
    " If this is a medical emergency, please hang up and call 911. I can take a message for our"
    " team."
)
SSN_ASK = "Thank you. Now please say only the last four digits of your Social Security number."
VERIFIED_SARAH = "Thank you, Sarah. You're verified. How can I help you today?"
EMERGENCY = (
    "If you're experiencing a medical emergency, please hang up and call 911 or go to the"
    " nearest emergency room right away."
)
FALLBACK = "I'm sorry, I can't help with that. I can take a message for our staff instead."
ADVICE_REFUSAL = "I'm not able to give medical advice. I can send a message to your doctor instead."

CLAIM_TURN = "Hi, this is Sarah Johnson, J O H N S O N, born March 14, 1985."
SARAH_CLAIM = {"full_name": "Sarah Johnson", "dob": "1985-03-14"}
SARAH_APPOINTMENT = "Thursday, December 3 at 10:30 AM with Dr. Patel"
APPOINTMENT_REPLY = f"Your next appointment is {SARAH_APPOINTMENT}."
# Sarah's last four, as the caller says them, in two turns or one; built from pieces so that
# no file holds them.
FIRST_PAIR, SECOND_PAIR = "33", "44"
DIGITS_IN_WORDS = "three three four four"

INTAKE_TOOLS = ["verify_identity"]
SERVING_TOOLS = [
    "get_next_appointment",
    "request_refill",
    "get_lab_status",
    "get_balance",
    "send_message_to_doctor",
]

EVENTS = text("SELECT action, resource_type, resource_id, outcome FROM audit_events ORDER BY seq")
EVENTS_WITH_REASONS = text(
    "SELECT action, resource_type, resource_id, outcome, reason FROM audit_events ORDER BY seq"
)


# --- the scripted model -----------------------------------------------------------------


def says(words: str) -> ModelReply:
    return ModelReply(
        stop_reason="end_turn",
        content=[{"type": "text", "text": words}],
        text=words,
        tool_calls=[],
    )


def calls(*tools: tuple[str, object], words: str = "") -> ModelReply:
    """A reply that asks for tools, each with an id of its own."""
    tool_calls = [
        ToolCall(id=f"toolu_{index}", name=n, arguments=a) for index, (n, a) in enumerate(tools)
    ]
    content: list[dict[str, Any]] = [{"type": "text", "text": words}] if words else []
    content += [
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        for call in tool_calls
    ]
    return ModelReply(stop_reason="tool_use", content=content, text=words, tool_calls=tool_calls)


def refuses() -> ModelReply:
    """A refusal that came after some words: they are not used."""
    words = "I can help with"
    return ModelReply(
        stop_reason="refusal",
        content=[{"type": "text", "text": words}],
        text=words,
        tool_calls=[],
    )


def cut_off() -> ModelReply:
    """A reply cut off at max_tokens, mid-sentence: not used."""
    words = "Your next appointment is on"
    return ModelReply(
        stop_reason="max_tokens",
        content=[{"type": "text", "text": words}],
        text=words,
        tool_calls=[],
    )


Step = ModelReply | Callable[[asyncio.Event], Awaitable[ModelReply]]


async def api_error(first_token: asyncio.Event) -> ModelReply:
    raise RuntimeError("the API answered 529")


async def never_starts(first_token: asyncio.Event) -> ModelReply:
    await asyncio.sleep(5)
    raise AssertionError("a reply that never starts is not waited for")


async def stalls(first_token: asyncio.Event) -> ModelReply:
    first_token.set()
    await asyncio.sleep(5)
    raise AssertionError("a reply that stalls is not waited for")


class ScriptedModel:
    """Answers each request with the next step and keeps a copy of every request."""

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.requests: list[ModelRequest] = []

    async def reply(self, request: ModelRequest, first_token: asyncio.Event) -> ModelReply:
        self.requests.append(copy.deepcopy(request))
        assert self.steps, "the model was asked more often than the test expected"
        step = self.steps.pop(0)
        if isinstance(step, ModelReply):
            first_token.set()
            return step
        return await step(first_token)

    def then(self, *steps: Step) -> None:
        self.steps.extend(steps)


def tool_names(request: ModelRequest) -> list[str]:
    return [tool["name"] for tool in request.tools]


def everything_sent(model: ScriptedModel) -> str:
    return json.dumps([[r.system, r.messages, r.tools] for r in model.requests])


def tool_results(request: ModelRequest) -> list[dict[str, Any]]:
    """The tool results of the request's last message, their content parsed."""
    last = request.messages[-1]
    assert last["role"] == "user"
    return [
        {**block, "content": json.loads(block["content"])}
        for block in last["content"]
        if block["type"] == "tool_result"
    ]


def assert_well_formed(messages: list[dict[str, Any]]) -> None:
    """A conversation the Messages API takes: it starts with the caller, and every tool the
    model asked for has its result in the next message."""
    assert messages[0] == {"role": "user", "content": CALL_CONNECTED}
    assert messages[-1]["role"] == "user"
    for index, message in enumerate(messages):
        content = message["content"]
        asked = (
            []
            if isinstance(content, str)
            else [b["id"] for b in content if b["type"] == "tool_use"]
        )
        if asked:
            answered = messages[index + 1]
            assert answered["role"] == "user"
            assert [b["tool_use_id"] for b in answered["content"]] == asked


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture
def registry() -> Registry:
    return register_tools()


def deps_on(engine: AsyncEngine, at: datetime = T0) -> Deps:
    return Deps(
        sessions=create_session_factory(engine),
        audit=AuditLog(SecretStr(TEST_AUDIT_KEY)),
        now=lambda: at,
        claim_keys=ClaimKeys(SecretStr(TEST_IDENTITY_KEY)),
    )


@pytest.fixture
async def seeded(owner_session: AsyncSession) -> None:
    await seed(owner_session, load_seed_data(), SEED_DAY)
    await owner_session.commit()


@pytest.fixture
def deps(runtime: AsyncEngine, seeded: None) -> Deps:
    return deps_on(runtime)


@pytest.fixture
async def offline_deps() -> AsyncIterator[Deps]:
    """For turns that never reach a tool: an engine that is never connected."""
    engine = create_database_engine(offline_settings())
    try:
        yield deps_on(engine)
    finally:
        await engine.dispose()


def rows(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(EVENTS)]


def new_call_id() -> str:
    call_id = CallRegistry(max_calls=1, max_seconds=1).reserve()
    assert call_id is not None
    return call_id


# A call id of the registry's shape that holds none of the text the log test looks for.
FIXED_CALL_ID = "0f1e2d3c4b5a46978a1b2c3d4e5f6a7b"


def agent_for(
    model: ScriptedModel, deps: Deps, registry: Registry, call_id: str | None = None, **options: Any
) -> CallAgent:
    agent, opening = start_call(call_id or new_call_id(), deps, model, registry, **options)
    assert opening == GREETING
    return agent


async def verified(agent: CallAgent, model: ScriptedModel) -> None:
    """The caller claims to be Sarah and says her last four."""
    model.then(calls(("verify_identity", SARAH_CLAIM)))
    assert (await agent.handle(CLAIM_TURN)).lines == (SSN_ASK,)
    assert (await agent.handle(DIGITS_IN_WORDS)).lines == (VERIFIED_SARAH,)
    assert agent.state.phase is Phase.SERVING


# --- R3 ---------------------------------------------------------------------------------


async def test_verify_then_next_appointment(
    deps: Deps, registry: Registry, owner_engine: Engine
) -> None:
    model = ScriptedModel(
        calls(("verify_identity", SARAH_CLAIM)),
        calls(("get_next_appointment", {})),
        says(APPOINTMENT_REPLY),
    )
    agent, opening = start_call(new_call_id(), deps, model, registry)
    assert opening == GREETING
    assert agent.state.phase is Phase.INTAKE

    claimed = await agent.handle(CLAIM_TURN)
    assert claimed.action is GuardAction.PASS
    assert claimed.shown == CLAIM_TURN
    assert claimed.lines == (SSN_ASK,)
    assert claimed.hand_off is False
    assert agent.state.expecting_ssn4

    digits = await agent.handle(DIGITS_IN_WORDS)
    assert digits.action is GuardAction.SWALLOW
    assert digits.shown is None
    assert digits.lines == (VERIFIED_SARAH,)
    assert (agent.state.phase, agent.state.patient_id) == (Phase.SERVING, "P001")

    asked = await agent.handle("When is my next appointment?")
    assert asked.shown == "When is my next appointment?"
    assert asked.lines == (APPOINTMENT_REPLY,)
    assert asked.timings.tool_rounds == 1

    # The model was told what the tool returned, and nothing else about Sarah.
    assert tool_results(model.requests[2]) == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_0",
            "content": {
                "status": "done",
                "message": None,
                "data": {"appointment": SARAH_APPOINTMENT},
            },
            "is_error": False,
        }
    ]
    assert rows(owner_engine) == [
        ("identity_claimed", "tool", "verify_identity", "success"),
        ("identity_verified", "patient", "P001", "success"),
        ("appointment_read", "patient", "P001", "success"),
    ]
    assert_well_formed(model.requests[-1].messages)


# --- R1 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [[FIRST_PAIR, SECOND_PAIR], [FIRST_PAIR + SECOND_PAIR], ["three three", "four four"]],
    ids=["numerals-in-two", "numerals-in-one", "words-in-two"],
)
async def test_swallowed_turns_never_reach_the_model(
    deps: Deps, registry: Registry, said: list[str]
) -> None:
    model = ScriptedModel(calls(("verify_identity", SARAH_CLAIM)))
    agent = agent_for(model, deps, registry)
    await agent.handle(CLAIM_TURN)
    asked_before = len(model.requests)

    results = [await agent.handle(turn) for turn in said]

    assert len(model.requests) == asked_before
    assert [result.action for result in results] == [GuardAction.SWALLOW] * len(said)
    assert [result.shown for result in results] == [None] * len(said)
    assert [result.lines for result in results] == [()] * (len(said) - 1) + [(VERIFIED_SARAH,)]

    model.then(says("Sure, what would you like to know?"))
    await agent.handle("Can you help me with something?")
    sent = everything_sent(model)
    for turn in said:
        assert turn not in sent
    assert FIRST_PAIR + SECOND_PAIR not in sent
    # The conversation holds the scripted lines, never the swallowed turns.
    assert [m["content"] for m in model.requests[-1].messages if isinstance(m["content"], str)] == [
        CALL_CONNECTED,
        GREETING,
        CLAIM_TURN,
        SSN_ASK,
        VERIFIED_SARAH,
        "Can you help me with something?",
    ]


# --- R2 ---------------------------------------------------------------------------------


async def test_model_gets_only_the_gated_tools(
    deps: Deps, registry: Registry, owner_engine: Engine
) -> None:
    # In intake the model asks for the balance anyway: dispatch refuses it.
    model = ScriptedModel(
        calls(("get_balance", {})),
        says("I need to verify you first. What is your full name and date of birth?"),
    )
    agent = agent_for(model, deps, registry)

    refused = await agent.handle("What do I owe?")

    assert refused.lines == (
        "I need to verify you first. What is your full name and date of birth?",
    )
    assert [tool_names(request) for request in model.requests] == [INTAKE_TOOLS, INTAKE_TOOLS]
    assert tool_results(model.requests[1]) == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_0",
            "content": {
                "status": "refused",
                "message": "That is not available right now.",
                "data": {},
            },
            "is_error": True,
        }
    ]

    await verified(agent, model)
    model.then(calls(("get_balance", {})), says("You owe forty-five dollars."))
    await agent.handle("What do I owe?")

    assert [tool_names(request) for request in model.requests] == [
        INTAKE_TOOLS,
        INTAKE_TOOLS,
        INTAKE_TOOLS,
        SERVING_TOOLS,
        SERVING_TOOLS,
    ]
    # Each offered as the registry declares it: its arguments' schema and its description.
    schemas = {tool.name: tool.arguments.model_json_schema() for tool in registry.tools}
    for request in model.requests:
        assert request.tools == [
            {"name": name, "description": TOOL_DESCRIPTIONS[name], "input_schema": schemas[name]}
            for name in tool_names(request)
        ]
    assert tool_results(model.requests[-1])[0]["content"] == {
        "status": "done",
        "message": None,
        "data": {"balance": "45.00"},
    }
    # Every tool call went through dispatch, which leaves one event for each.
    assert rows(owner_engine) == [
        ("tool_refused", "tool", "get_balance", "refused"),
        ("identity_claimed", "tool", "verify_identity", "success"),
        ("identity_verified", "patient", "P001", "success"),
        ("balance_read", "patient", "P001", "success"),
    ]


async def test_a_closed_office_offers_no_tools(offline_deps: Deps, registry: Registry) -> None:
    closed = Deps(
        sessions=offline_deps.sessions,
        audit=offline_deps.audit,
        now=lambda: EVENING,
        claim_keys=offline_deps.claim_keys,
    )
    model = ScriptedModel(says("We open at nine tomorrow morning."))
    agent, opening = start_call(new_call_id(), closed, model, registry)

    await agent.handle("Can I check my balance?")

    assert opening == CLOSED
    assert agent.state.office_open is False
    assert [tool_names(request) for request in model.requests] == [[]]


# --- R4 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("armed", [False, True], ids=["intake", "ssn-step-armed"])
async def test_emergency_skips_the_model(
    offline_deps: Deps, registry: Registry, armed: bool
) -> None:
    state = move(new_call(call_id=new_call_id(), started_at=T0, office_open=True), Phase.INTAKE)
    if armed:
        state = claim(state, IdentityClaim(full_name="Sarah Johnson", dob=date(1985, 3, 14)))
    model = ScriptedModel()
    agent = CallAgent(state, offline_deps, model, registry)

    result = await agent.handle("I think I'm having a heart attack, three three")

    assert result.action is GuardAction.EMERGENCY
    assert result.shown is None
    assert result.lines == (EMERGENCY,)
    assert agent.state.phase is Phase.EMERGENCY
    assert agent.state.expecting_ssn4 is False
    assert model.requests == []


# --- R5 ---------------------------------------------------------------------------------


async def test_scripts_are_spoken_verbatim(
    deps: Deps, registry: Registry, owner_engine: Engine
) -> None:
    # The model writes words of its own next to the call, and asks for a second tool after
    # it: the scripted line is the reply, and the second tool does not run.
    model = ScriptedModel(
        calls(
            ("verify_identity", SARAH_CLAIM),
            ("get_balance", {}),
            words="Great, now tell me your whole social security number.",
        ),
        says("Of course."),
    )
    agent = agent_for(model, deps, registry)

    result = await agent.handle(CLAIM_TURN)

    assert result.lines == (SSN_ASK,)
    assert len(model.requests) == 1
    # The verified line comes from code, with the first name from the record.
    assert (await agent.handle(DIGITS_IN_WORDS)).lines == (VERIFIED_SARAH,)
    await agent.handle("Thanks.")
    after_claim = model.requests[1].messages
    claimed_at = next(i for i, m in enumerate(after_claim) if m["content"] == CLAIM_TURN)
    assert after_claim[claimed_at + 2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_0",
                "content": json.dumps({"status": "done", "said": "the system spoke the next line"}),
                "is_error": False,
            },
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": json.dumps({"status": "not_run", "said": "the turn had ended"}),
                "is_error": True,
            },
        ],
    }
    # The model is told the line was said, as its own message.
    assert after_claim[claimed_at + 3 : claimed_at + 5] == [
        {"role": "assistant", "content": SSN_ASK},
        {"role": "assistant", "content": VERIFIED_SARAH},
    ]
    assert rows(owner_engine) == [
        ("identity_claimed", "tool", "verify_identity", "success"),
        ("identity_verified", "patient", "P001", "success"),
    ]
    assert_well_formed(after_claim)


# --- R6 ---------------------------------------------------------------------------------


def too_many_rounds() -> list[Step]:
    """Five replies in a row that ask for a tool."""
    return [calls(("get_balance", {})) for _ in range(5)]


FAILURES: dict[str, Callable[[], list[Step]]] = {
    "refusal": lambda: [refuses()],
    "api-error": lambda: [api_error],
    "no-first-token": lambda: [never_starts],
    "stalled-reply": lambda: [stalls],
    "five-tool-rounds": too_many_rounds,
    "max-tokens": lambda: [cut_off()],
}
# The reason each failure is logged with.
REASONS = {
    "refusal": "refusal",
    "api-error": "request_failed",
    "no-first-token": "first_token_timeout",
    "stalled-reply": "reply_timeout",
    "five-tool-rounds": "tool_rounds",
    "max-tokens": "stop_reason",
}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("refusal", "refusal"),
        ("api-error", "api-error"),
        ("no-first-token", "no-first-token"),
        ("stalled-reply", "stalled-reply"),
        ("five-tool-rounds", "five-tool-rounds"),
        ("max-tokens", "max-tokens"),
        ("refusal", "api-error"),
        ("five-tool-rounds", "no-first-token"),
    ],
)
async def test_model_failures_fall_back(
    deps: Deps, registry: Registry, caplog: pytest.LogCaptureFixture, first: str, second: str
) -> None:
    caplog.set_level(logging.INFO)
    model = ScriptedModel(*FAILURES[first]())
    agent = agent_for(model, deps, registry, first_token_seconds=0.05, reply_seconds=0.05)

    once = await agent.handle("Can you help me?")

    assert once.lines == (FALLBACK,)
    assert once.hand_off is False

    model.then(*FAILURES[second]())
    twice = await agent.handle("Hello?")

    assert twice.lines == (FALLBACK,)
    assert twice.hand_off is True

    # The call goes on, and the conversation is still one the API takes.
    model.then(says("I'm here."))
    after = await agent.handle("Are you there?")
    assert after.lines == ("I'm here.",)
    assert after.hand_off is True
    assert_well_formed(model.requests[-1].messages)
    fallbacks = [
        m for m in model.requests[-1].messages if m == {"role": "assistant", "content": FALLBACK}
    ]
    assert len(fallbacks) == 2
    call_id = agent.state.call_id
    assert [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("model_fallback")
    ] == [
        f"model_fallback call_id={call_id} reason={REASONS[first]} failures=1 hand_off=False",
        f"model_fallback call_id={call_id} reason={REASONS[second]} failures=2 hand_off=True",
    ]


async def test_four_tool_rounds_are_allowed(deps: Deps, registry: Registry) -> None:
    model = ScriptedModel(*[calls(("get_balance", {})) for _ in range(4)], says("Done."))
    agent = agent_for(model, deps, registry)

    result = await agent.handle("Can you help me?")

    assert result.lines == ("Done.",)
    assert result.timings.tool_rounds == 4
    assert result.hand_off is False


# --- R7 ---------------------------------------------------------------------------------


async def test_turn_logs_carry_no_text(
    deps: Deps, registry: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    marker = "marker-model-words"
    model = ScriptedModel()
    agent = agent_for(model, deps, registry, FIXED_CALL_ID, first_token_seconds=0.05)

    await verified(agent, model)
    model.then(
        calls(("get_next_appointment", {})),
        says(f"{marker} {APPOINTMENT_REPLY}"),
        calls(("request_refill", {"medication_name": "marker-medicine", "confirmed": True})),
        says(marker),
        api_error,
        refuses(),
    )
    await agent.handle("When is my next appointment?")
    await agent.handle("Please refill my marker-medicine.")
    await agent.handle("marker-caller-words one")
    await agent.handle("marker-caller-words two")

    logged = caplog.text
    assert "model_fallback" in logged
    assert "tool_called" in logged
    for secret in (
        "Sarah",
        "Johnson",
        "1985",
        "J O H N",
        DIGITS_IN_WORDS,
        FIRST_PAIR + SECOND_PAIR,
        SARAH_APPOINTMENT,
        "December",
        marker,
        "marker-medicine",
        "marker-caller-words",
        "When is my next appointment",
        "529",
    ):
        assert secret not in logged, secret


# --- R8 ---------------------------------------------------------------------------------


def patient_values() -> set[str]:
    """Every value the seed holds about a patient, but the doctors, who are clinic facts."""
    values: set[str] = set()
    for patient in load_seed_data()["patients"]:
        for key in ("id", "first_name", "last_name", "dob", "ssn_last4", "phone"):
            values.add(str(patient[key]))
        values.add(str(patient["lab_results_status"]))
        values.add(f"{patient['balance_due']:.2f}")
        if patient["next_appointment"]:
            values.add(patient["next_appointment"].split(" with ")[0])
        for medication in patient["medications"]:
            values |= {medication["name"], medication["name"].split()[0], medication["pharmacy"]}
    return values


async def test_prompt_is_static(deps: Deps, registry: Registry) -> None:
    fixed_text = [SYSTEM_PROMPT, *TOOL_DESCRIPTIONS.values()]
    for value in patient_values():
        for piece in fixed_text:
            assert value.lower() not in piece.lower(), value
    for secret in (*VOICE_SECRETS.values(), TEST_AUDIT_KEY, TEST_IDENTITY_KEY):
        for piece in fixed_text:
            assert secret not in piece

    # The same prompt in every request, before verification and after.
    model = ScriptedModel(says("Hello."))
    agent = agent_for(model, deps, registry)
    await agent.handle("Hello.")
    await verified(agent, model)
    model.then(calls(("get_balance", {})), says("You owe forty-five dollars."))
    await agent.handle("What do I owe?")

    assert [request.system for request in model.requests] == [SYSTEM_PROMPT] * 4


def test_every_offered_tool_is_described() -> None:
    registry = register_tools()
    assert sorted(t.name for t in registry.tools if t.model_callable) == sorted(TOOL_DESCRIPTIONS)


# --- the output guard (spec 12: R9, R10) --------------------------------------------------


def rows_with_reasons(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(EVENTS_WITH_REASONS)]


async def test_blocked_text_goes_nowhere(
    deps: Deps, registry: Registry, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    marker = "marker-blocked-words"
    model = ScriptedModel()
    agent = agent_for(model, deps, registry, FIXED_CALL_ID)
    await verified(agent, model)
    model.then(
        says(f"Let me see. You should stop taking {marker} today. Then call us back."),
        says("Is there anything else?"),
    )

    result = await agent.handle("Should I keep taking my pills?")

    # What came before the blocked sentence is said; the sentence is replaced by its line and
    # the rest of the reply is dropped.
    assert result.lines == ("Let me see.", ADVICE_REFUSAL)
    assert result.withheld == "medical_advice"
    assert result.hand_off is False

    await agent.handle("No, thanks.")
    await agent.settle()
    # The conversation has what was said, never the blocked text or what came after it.
    sent = everything_sent(model)
    for blocked in (marker, "stop taking", "Then call us back"):
        assert blocked not in sent, blocked
    assert model.requests[-1].messages[-3:] == [
        {"role": "assistant", "content": "Let me see."},
        {"role": "assistant", "content": ADVICE_REFUSAL},
        {"role": "user", "content": "No, thanks."},
    ]
    assert_well_formed(model.requests[-1].messages)
    # The log has the rule's name, the call id and the count, never the text.
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("guard_")] == [
        f"guard_block call_id={FIXED_CALL_ID} rule=medical_advice blocks=1 hand_off=False"
    ]
    for blocked in (marker, "stop taking", "Then call us back", "Let me see"):
        assert blocked not in caplog.text, blocked
    # One audit event, the rule its reason; no text in it.
    events = rows_with_reasons(owner_engine)
    assert events[-1] == ("guard_block", "call", FIXED_CALL_ID, "refused", "medical_advice")
    assert [event[0] for event in events].count("guard_block") == 1
    assert marker not in json.dumps([[str(value) for value in event] for event in events])


async def test_two_blocks_hand_off(
    deps: Deps, registry: Registry, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    model = ScriptedModel(
        says("It's probably just a cold."),
        says("Our staff can help with that."),
        says("You should skip your next dose."),
        says("That's nothing to worry about."),
        says("I can take a message."),
    )
    agent = agent_for(model, deps, registry)

    turns = [
        await agent.handle(caller)
        for caller in ("I feel sick.", "What now?", "Can I skip it?", "Is that bad?", "Okay.")
    ]

    assert [(turn.lines, turn.withheld, turn.hand_off) for turn in turns] == [
        ((ADVICE_REFUSAL,), "medical_advice", False),
        # An allowed reply in between: the count starts again.
        (("Our staff can help with that.",), None, False),
        ((ADVICE_REFUSAL,), "medical_advice", False),
        # Two blocks in a row.
        ((ADVICE_REFUSAL,), "medical_advice", True),
        # The hand-off, once asked for, stays asked for.
        (("I can take a message.",), None, True),
    ]
    call_id = agent.state.call_id
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("guard_")] == [
        f"guard_block call_id={call_id} rule=medical_advice blocks=1 hand_off=False",
        f"guard_block call_id={call_id} rule=medical_advice blocks=1 hand_off=False",
        f"guard_block call_id={call_id} rule=medical_advice blocks=2 hand_off=True",
    ]
    await agent.settle()
    assert (
        rows_with_reasons(owner_engine)
        == [("guard_block", "call", call_id, "refused", "medical_advice")] * 3
    )


# --- R10 --------------------------------------------------------------------------------


def server_sent(*events: dict[str, Any]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def streamed_reply(model_id: str) -> bytes:
    """A thinking block, a sentence and a tool call, as the Messages API streams them."""
    return server_sent(
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model_id,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "c2lnbmF0dXJl"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "One "},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "moment."},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_x",
                "name": "get_balance",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"type": "message_delta", "stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 9},
        },
        {"type": "message_stop"},
    )


@pytest.mark.parametrize("model_id", [None, "claude-sonnet-5-5"], ids=["default", "configured"])
async def test_model_request_shape(model_id: str | None) -> None:
    settings = offline_settings() if model_id is None else offline_settings(llm_model=model_id)
    expected_model = "claude-haiku-5-5" if model_id is None else model_id
    sent: list[Request] = []

    def answer(request: Request) -> Response:
        sent.append(request)
        return Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=streamed_reply(expected_model),
        )

    async with AsyncClient(transport=MockTransport(answer)) as http_client:
        model = anthropic_model(settings, http_client=http_client)
        assert model is not None
        request = ModelRequest(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "What do I owe?"}],
            tools=[{"name": "get_balance", "description": "d", "input_schema": {"type": "object"}}],
        )
        first_token = asyncio.Event()
        reply = await model.reply(request, first_token)

    (only,) = sent
    body = json.loads(only.content)
    assert only.url.path == "/v1/messages"
    # anthropic 2.x sends the key as a bearer token.
    assert only.headers["authorization"] == f"Bearer {VOICE_SECRETS['ANTHROPIC_API_KEY']}"
    assert body == {
        "model": expected_model,
        "max_tokens": 300,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": "What do I owe?"}],
        "tools": [{"name": "get_balance", "description": "d", "input_schema": {"type": "object"}}],
        # A phone turn does not think: no thinking before the first word, none counted
        # against max_tokens. Haiku 5.5 accepts thinking off at effort low.
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low"},
        "stream": True,
    }
    for sampling in ("temperature", "top_p", "top_k"):
        assert sampling not in body

    assert first_token.is_set()
    assert reply == ModelReply(
        stop_reason="tool_use",
        content=[
            {"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl"},
            {"type": "text", "text": "One moment."},
            {"type": "tool_use", "id": "toolu_x", "name": "get_balance", "input": {}},
        ],
        text="One moment.",
        tool_calls=[ToolCall(id="toolu_x", name="get_balance", arguments={})],
    )


def test_no_model_without_a_key() -> None:
    assert anthropic_model(offline_settings(app_env="dev", anthropic_api_key=None)) is None
