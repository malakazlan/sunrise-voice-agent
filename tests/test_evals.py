"""The scenario evals (evals/): every scenario file runs through the live turn loop with its
scripted model against a database of its own, and every kind of expectation can fail. The
scenario names, the expected failures and the report are written by hand from the scenario
files and the spec; nothing reaches the network."""

import ast
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import offline_settings

import app.agent.turn
import app.voice.pipeline
import evals.runner
from app.agent.model import AnthropicModel
from app.agent.scripts import ScriptId
from app.agent.state import Phase
from app.audit.log import Action
from app.config import Settings
from app.guard.input import Flag
from evals.database import EvalServer, eval_server, eval_settings
from evals.runner import (
    Failure,
    RealModeRefusedError,
    main,
    real_model,
    report,
    run_all,
    run_scenario,
)
from evals.scenario import (
    RowExpect,
    SayStep,
    Scenario,
    ScenarioError,
    ToolExpect,
    TurnExpect,
    load_scenario,
    scenario_files,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = REPO_ROOT / "evals" / "scenarios"
RUNNER = REPO_ROOT / "evals" / "runner.py"

# The spec's scenarios, one file each.
SCENARIOS = [
    "s01_verify_refill",
    "s02_verify_fail_lock",
    "s03_new_patient_booking",
    "s04_after_hours_message",
    "s05_medical_advice",
    "s06_emergency",
    "s07_third_party",
    "s08_injection",
    "s09_zero_refills",
    "s10_emergency_after_hours",
    "s11_early_ssn",
    "s12_unknown_caller",
    "s13_emergency_paraphrase",
]


@pytest.fixture(scope="module")
def server() -> Iterator[EvalServer]:
    with eval_server(eval_settings()) as made:
        yield made


def refill() -> Scenario:
    """s01: Sarah is verified, then asks for a Lisinopril refill and confirms it."""
    return load_scenario(SCENARIO_DIR / "s01_verify_refill.yaml")


def at_end(scenario: Scenario, **changes: Any) -> Scenario:
    return scenario.model_copy(update={"expect": scenario.expect.model_copy(update=changes)})


def at_turn(scenario: Scenario, index: int, **changes: Any) -> Scenario:
    turns = list(scenario.turns)
    turn = turns[index]
    expect = TurnExpect() if turn.expect is None else turn.expect
    turns[index] = turn.model_copy(update={"expect": expect.model_copy(update=changes)})
    return scenario.model_copy(update={"turns": turns})


# --- R1 ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_every_scenario_runs(server: EvalServer) -> None:
    # Read here, apart from the runner's own listing: every file in the folder is a scenario.
    assert sorted(path.name for path in SCENARIO_DIR.iterdir()) == [
        f"{name}.yaml" for name in SCENARIOS
    ]

    results = await run_all(scenario_files(), server, model=None, runs=1)

    assert sorted(results) == SCENARIOS
    assert {name: runs for name, runs in results.items() if runs != [[]]} == {}


VALID = """\
now: "2026-11-24T10:00:00-05:00"
turns:
  - caller: "Hello."
    model:
      - say: "Hello. How can I help you?"
    expect:
      said: []
expect:
  said: [greeting]
"""

# Each a valid file with one change, and a piece of the error it must give.
BROKEN = {
    "unknown-top-level-key": (VALID + "title: x\n", "Extra inputs are not permitted"),
    "unknown-turn-key": (
        VALID.replace('  - caller: "Hello."\n', '  - caller: "Hello."\n    mood: calm\n'),
        "Extra inputs are not permitted",
    ),
    "unknown-expectation": (
        VALID.replace("  said: [greeting]", "  sayd: [greeting]"),
        "Extra inputs are not permitted",
    ),
    "unknown-step-key": (
        VALID.replace('      - say: "Hello. How can I help you?"', '      - shout: "Hello."'),
        "say",
    ),
    "empty-expectations": (
        VALID.replace("expect:\n  said: [greeting]\n", "expect: {}\n"),
        "states at least one expectation",
    ),
    "no-expectations": (
        VALID.replace("expect:\n  said: [greeting]\n", ""),
        "Field required",
    ),
    "empty-turn-expectations": (
        VALID.replace("    expect:\n      said: []\n", "    expect: {}\n"),
        "states at least one expectation",
    ),
    "null-expectation": (
        VALID.replace("  said: [greeting]", "  said:"),
        "is given no value",
    ),
    "duplicate-key": (VALID + "expect:\n  phase: intake\n", "given twice"),
    "call-and-say": (
        VALID.replace(
            '      - say: "Hello. How can I help you?"',
            '      - say: "Hello."\n        call: close_call',
        ),
        "Extra inputs are not permitted",
    ),
    "no-turns": (
        VALID.replace(VALID[VALID.index("turns:") : VALID.index("expect:\n  said")], "turns: []\n"),
        "at least 1 item",
    ),
    "naive-clock": (
        VALID.replace('"2026-11-24T10:00:00-05:00"', '"2026-11-24T10:00:00"'),
        "timezone",
    ),
    "unknown-line": (VALID.replace("[greeting]", "[hello]"), "said.0"),
    "unknown-tool": (
        VALID + "  tools:\n    - call: read_all_patients\n",
        "not a tool the model is offered",
    ),
    "code-only-tool": (
        VALID + "  never_offered: [submit_identity]\n",
        "not a tool the model is offered",
    ),
    "empty-never": (VALID + "  never: []\n", "at least 1 item"),
    "unknown-table": (
        VALID + "  rows:\n    - table: audit_events\n      count: 0\n",
        "not a table the evals read",
    ),
    "unknown-column": (
        VALID + "  rows:\n    - table: requests\n      where: {kind: refill}\n      count: 0\n",
        "has no column kind",
    ),
    "wrong-value-type": (
        VALID
        + "  rows:\n    - table: available_slots\n      where: {is_available: 'no'}\n"
        + "      count: 0\n",
        "is_available",
    ),
    "unreadable-column": (
        VALID
        + "  rows:\n    - table: patients\n      where: {dob: '1985-03-14'}\n      count: 0\n",
        "cannot be compared",
    ),
}


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_scenario_schema_is_strict(tmp_path: Path, case: str) -> None:
    valid = tmp_path / "valid.yaml"
    valid.write_text(VALID, encoding="utf-8")
    assert load_scenario(valid).expect.said == [ScriptId.GREETING]

    text, message = BROKEN[case]
    broken = tmp_path / f"{case}.yaml"
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ScenarioError, match=message):
        load_scenario(broken)


# --- R2 ---------------------------------------------------------------------------------


def unused_step(scenario: Scenario) -> Scenario:
    """The last turn's script has one reply more than the turn asks for."""
    turns = list(scenario.turns)
    last = turns[-1]
    turns[-1] = last.model_copy(update={"model": [*last.model, SayStep(say="Anything else?")]})
    return scenario.model_copy(update={"turns": turns})


# Each break: the copy of s01, and the one failure it must give (scope, kind).
BREAKS: dict[str, tuple[Callable[[Scenario], Scenario], tuple[str, str]]] = {
    "said": (
        lambda s: at_end(s, said=[ScriptId.GREETING, ScriptId.SSN_ASK]),
        ("call", "said"),
    ),
    "tools-arguments": (
        lambda s: at_turn(
            s, 2, tools=[ToolExpect(call="request_refill", args={"confirmed": True})]
        ),
        ("turn 3", "tools"),
    ),
    "tools-returns": (
        lambda s: at_turn(
            s,
            3,
            tools=[
                ToolExpect(
                    call="request_refill",
                    args={"confirmed": True},
                    returns={"status": "sent_to_doctor"},
                )
            ],
        ),
        ("turn 4", "tools"),
    ),
    "phase": (lambda s: at_end(s, phase=Phase.INTAKE), ("call", "phase")),
    "verified": (lambda s: at_end(s, verified=False), ("call", "verified")),
    "rows": (
        lambda s: at_end(s, rows=[RowExpect(table="requests", where={"type": "refill"}, count=2)]),
        ("call", "rows"),
    ),
    "audit": (
        lambda s: at_end(
            s,
            audit=[Action.IDENTITY_CLAIMED, Action.IDENTITY_VERIFIED, Action.REFILL_REQUESTED],
        ),
        ("call", "audit"),
    ),
    "never": (lambda s: at_end(s, never=["Lisinopril 10mg"]), ("call", "never")),
    "offered": (lambda s: at_turn(s, 2, offered=["verify_identity"]), ("turn 3", "offered")),
    "never-offered": (
        lambda s: at_turn(s, 2, never_offered=["request_refill"]),
        ("turn 3", "never_offered"),
    ),
    "flags": (lambda s: at_turn(s, 2, flags=[Flag.THIRD_PARTY]), ("turn 3", "flags")),
    "reaches-model": (
        lambda s: at_turn(s, 1, reaches_model=True),
        ("turn 2", "reaches_model"),
    ),
    "model-script": (unused_step, ("turn 4", "model_script")),
}


@pytest.mark.anyio
@pytest.mark.parametrize("kind", sorted(BREAKS))
async def test_each_expectation_kind_can_fail(server: EvalServer, kind: str) -> None:
    assert await run_scenario(refill(), server) == []

    broken, expected = BREAKS[kind]
    failures = await run_scenario(broken(refill()), server)

    assert [(failure.scope, failure.kind) for failure in failures] == [expected]


# --- R3 ---------------------------------------------------------------------------------

LIVE_NAMES = {"CallAgent", "start_call", "handle", "guard_input", "call_tool", "submit_claim"}


@pytest.mark.anyio
async def test_runner_uses_the_live_code(server: EvalServer) -> None:
    # The very functions a live call starts with (app/voice/pipeline.py) and its guard.
    runner, pipeline, turn = vars(evals.runner), vars(app.voice.pipeline), vars(app.agent.turn)
    assert runner["start_call"] is pipeline["start_call"]
    assert runner["register_tools"] is pipeline["register_tools"]
    assert runner["guard_input"] is turn["guard_input"]

    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    assert defined & LIVE_NAMES == set()
    # Nothing private of another object is reached, and nothing is rebound on one.
    private = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr.startswith("_")
        and not (node.attr.startswith("__") and node.attr.endswith("__"))
        and not (isinstance(node.value, ast.Name) and node.value.id == "self")
    ]
    assert private == []
    rebound = [
        ast.unparse(target)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Attribute)
        and not (isinstance(target.value, ast.Name) and target.value.id == "self")
    ]
    assert rebound == []
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "setattr"
    ]

    # The SSN turn is swallowed by the input guard: the model never sees it.
    assert await run_scenario(refill(), server) == []


# --- R4 ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_real_mode_is_manual_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Under pytest the command refuses before it reads any settings.
    def no_settings() -> Settings:
        raise AssertionError("settings were read")

    monkeypatch.setattr(evals.runner, "Settings", no_settings)
    assert main(["--model", "real", "--runs", "3"]) == 2
    assert "never under pytest or in CI" in capsys.readouterr().err

    with_key = offline_settings(app_env="dev")
    # CI set at all, even empty, is CI.
    for environ in ({"PYTEST_CURRENT_TEST": "tests/test_evals.py"}, {"CI": "true"}, {"CI": ""}):
        with pytest.raises(RealModeRefusedError, match="never under pytest or in CI"):
            real_model(no_settings, environ)
    without_key = offline_settings(app_env="dev", anthropic_api_key=None)
    with pytest.raises(RealModeRefusedError, match="ANTHROPIC_API_KEY"):
        real_model(lambda: without_key, {})

    # By hand, with a key: the model of the settings. Nothing is sent.
    model = real_model(lambda: with_key, {})
    assert isinstance(model, AnthropicModel)
    await model.close()

    # And CI never asks for it.
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--model real" not in workflow


def test_report_names_the_first_unmet_expectation() -> None:
    results: dict[str, list[list[Failure]]] = {
        "s01_verify_refill": [[], [Failure("turn 2", "said", "d"), Failure("call", "rows", "d")]],
        "s06_emergency": [[], []],
    }

    text, failed = report(results)

    assert failed is True
    assert text.splitlines() == [
        "scenario           passed  first unmet expectation",
        "s01_verify_refill  1/2     run 2: turn 2 said",
        "s06_emergency      2/2",
    ]
    assert report({"s06_emergency": [[]]}) == (
        "scenario       passed  first unmet expectation\ns06_emergency  1/1\n",
        False,
    )


# --- R5 ---------------------------------------------------------------------------------


def with_caller(scenario: Scenario, index: int, caller: str) -> Scenario:
    turns = list(scenario.turns)
    turns[index] = turns[index].model_copy(update={"caller": caller})
    return scenario.model_copy(update={"turns": turns})


@pytest.mark.anyio
async def test_forbidden_strings_cover_both_sides(server: EvalServer) -> None:
    # What the caller said reached the model, and the agent never said it; in another case.
    shown_only = at_end(refill(), never=["NEED A REFILL OF MY"])
    assert await run_scenario(shown_only, server) == [
        Failure("call", "never", "'NEED A REFILL OF MY' was shown to the model")
    ]

    # The last line of the call: said, and never shown to the model after.
    said_only = at_end(refill(), never=["refill request has been sent"])
    assert await run_scenario(said_only, server) == [
        Failure("call", "never", "'refill request has been sent' was said")
    ]

    # Said in both: the reply read back what the tool returned.
    both = at_end(refill(), never=["CVS Main St"])
    assert await run_scenario(both, server) == [
        Failure("call", "never", "'CVS Main St' was said and shown to the model")
    ]

    # A string of digit words is looked for as the digits it says too.
    in_numerals = at_end(
        with_caller(refill(), 2, "I need a refill of my Lisinopril, bottle 2030."),
        never=["two zero three zero"],
    )
    assert await run_scenario(in_numerals, server) == [
        Failure("call", "never", "'two zero three zero' was shown to the model")
    ]
