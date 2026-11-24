"""Scenario files (evals/scenarios/*.yaml): the call's clock, the caller's turns, the model's side
for scripted mode, and what must come of the call. Data only, read strictly: an unknown key, a
key given twice, an expectation block with nothing in it, a key with no value, a tool the model
is never offered, or a table, column or value the evals cannot compare, and the file is refused.

A turn's `model` is the scripted model's replies to the requests the turn makes, in order:
`call` (a tool, with `args`) or `say` (a plain answer). The real model ignores them.

Expectations, checked after the turn they are under, and over the whole call at the end:
- said: the scripted lines said, by id, in order (the model's own words are not counted);
- tools: the tools the model asked for, in order, each by name; the arguments named under
  `args` equal the ones it gave, and the values named under `returns` equal those in the data
  the tool returned;
- phase, verified: the call's state at that point;
- rows: the rows the call has written so far (added, or changed from the seed) in a table that
  hold the given column values: exactly `count` of them;
- audit: the actions of the call's audit events, in order;
- never: strings found, whatever their case, in nothing said and nothing the model was shown;
  one written in digit words alone is looked for as its digits too;
- offered: the tools every model request offered;
- never_offered: tools no model request offered.
`offered` and `never_offered` need at least one model request. A turn can also state:
- flags: the input guard's flags for the turn;
- reaches_model: whether the model was shown the turn. A turn that does not reach it appears
  in nothing the model is shown, then or later.
"""

from pathlib import Path
from typing import Annotated, Any, Self

import yaml  # type: ignore[import-untyped]  # PyYAML ships no type information
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)
from sqlalchemy import Table

from app.agent.scripts import ScriptId
from app.agent.state import Phase
from app.agent.tools import register_tools
from app.audit.log import Action
from app.db.models import Base
from app.guard.input import Flag

SCENARIO_DIR = Path(__file__).with_name("scenarios")

# The tables a call writes to, besides the audit log (the `audit` expectation reads that).
TABLES: dict[str, Table] = {
    name: Base.metadata.tables[name]
    for name in (
        "patients",
        "appointments",
        "available_slots",
        "requests",
        "messages",
        "verification_attempts",
    )
}
# The kinds of column a row expectation compares.
COMPARABLE = (bool, int, str)


class ScenarioError(ValueError):
    """A scenario file that is not one."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    @model_validator(mode="after")
    def _no_empty_keys(self) -> Self:
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} is given no value")
        return self


def _model_tool(name: str) -> str:
    if name not in {tool.name for tool in register_tools().tools if tool.model_callable}:
        raise ValueError(f"{name} is not a tool the model is offered")
    return name


ToolName = Annotated[str, AfterValidator(_model_tool)]
Text = Annotated[str, Field(min_length=1)]


class CallStep(_Strict):
    call: Text
    args: dict[str, JsonValue] = {}


class SayStep(_Strict):
    say: Text


class ToolExpect(_Strict):
    call: ToolName
    args: dict[str, JsonValue] = {}
    returns: dict[str, JsonValue] | None = None


class RowExpect(_Strict):
    table: str
    where: dict[str, StrictBool | StrictInt | StrictStr] = {}
    count: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _comparable(self) -> Self:
        table = TABLES.get(self.table)
        if table is None:
            raise ValueError(f"{self.table} is not a table the evals read")
        for name, value in self.where.items():
            if name not in table.columns:
                raise ValueError(f"{self.table} has no column {name}")
            kind = table.columns[name].type.python_type
            if kind not in COMPARABLE:
                raise ValueError(f"{self.table}.{name} cannot be compared")
            if not isinstance(value, kind) or isinstance(value, bool) != (kind is bool):
                raise ValueError(f"{self.table}.{name} needs a {kind.__name__}")
        return self


class Expect(_Strict):
    said: list[ScriptId] | None = None
    tools: list[ToolExpect] | None = None
    phase: Phase | None = None
    verified: StrictBool | None = None
    rows: list[RowExpect] | None = Field(default=None, min_length=1)
    audit: list[Action] | None = None
    never: list[Text] | None = Field(default=None, min_length=1)
    offered: list[ToolName] | None = None
    never_offered: list[ToolName] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _something(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("an expectation block states at least one expectation")
        return self


class TurnExpect(Expect):
    flags: list[Flag] | None = None
    reaches_model: StrictBool | None = None


class Turn(_Strict):
    caller: Text
    model: list[CallStep | SayStep] = []
    expect: TurnExpect | None = None


class Scenario(_Strict):
    now: AwareDatetime
    turns: list[Turn] = Field(min_length=1)
    expect: Expect


def _refuse_repeated_keys(node: Any) -> None:
    """YAML takes the last of two equal keys; a scenario refuses them."""
    if isinstance(node, yaml.MappingNode):
        keys = [key.value for key, _value in node.value if isinstance(key, yaml.ScalarNode)]
        if len(keys) != len(set(keys)):
            raise ScenarioError(f"a key is given twice, line {node.start_mark.line + 1}")
        for key, value in node.value:
            _refuse_repeated_keys(key)
            _refuse_repeated_keys(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _refuse_repeated_keys(item)


def load_scenario(path: Path) -> Scenario:
    text = path.read_text(encoding="utf-8")
    try:
        _refuse_repeated_keys(yaml.compose(text, Loader=yaml.SafeLoader))
        data: object = yaml.safe_load(text)
        return Scenario.model_validate(data)
    except (ScenarioError, yaml.YAMLError, ValidationError) as error:
        raise ScenarioError(f"{path.name}: {error}") from None


def scenario_files() -> list[Path]:
    """Every scenario file, in name order."""
    return sorted(SCENARIO_DIR.glob("*.yaml"))
