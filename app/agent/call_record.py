"""The record of a call that has ended: one row of call_sessions (app/db/models.py) with when it
started and ended, whether the office was open, whether the caller was verified and as which
patient, why the call ended, how many turns the caller took, and the transcript.

The transcript is the history CallAgent keeps for the record (CallAgent.history in
app/agent/turn.py), one line a message: each caller turn as the input guard passed it to the
model, the fixed SWALLOWED_TURN or EMERGENCY_TURN marker where the model was given nothing of a
turn, and each line the agent said, as far as the caller heard it. The tool calls with any
text the model wrote beside them (never said, never through the output guard) and the tool
results are not in it.

It is masked three times before it is stored. First by `mask` (SSNs, dates, phone numbers,
email addresses). Then every name the call itself knows is replaced by <PERSON>, as whole words
in any case and spelled out letter by letter: the verified patient's first and last name, read
from their patients row (if that read fails, the first name the state holds), and each name
given to a tool in the call (NAME_ARGUMENTS), whether the tool accepted it or not. Then Presidio
with a small spaCy English model (names, locations and dates in free text). All of it but the
patients read runs on one worker thread of its own, never on the event loop that carries the
calls. Presidio is imported and its model loaded there, on first use, never at start-up, and
the model is never downloaded. If the Presidio pass fails, the text the first two passes gave
is stored, `names_masked` is false, and the failure is logged. The text is cut at a line
boundary to at most MAX_TRANSCRIPT characters before the passes, so they never read more than
that, and again after them, since a pass may lengthen it; the row says whether it was cut.

`record_call` takes an ended call and returns at once; the work runs in a task of its own that
nobody waits for. A call is stored once: a second record of it is dropped by the database and
logged as a duplicate. A record that fails is logged and dropped. `drain_call_records` waits for
the records still being made, for tests and for shutdown.

Logs carry event names, call ids, counts and exception class names; never text or a name.
"""

import asyncio
import functools
import importlib.util
import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.endings import END_PHASES
from app.agent.state import CallState, Phase
from app.agent.tool_registry import Deps
from app.audit.log import Reason
from app.db.models import MAX_TRANSCRIPT, CallSession, Patient
from app.guard.logging import quiet_presidio
from app.guard.mask import hide_names, mask

if TYPE_CHECKING:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine

logger = logging.getLogger(__name__)

# The small English pipeline, installed from the lock (pyproject.toml), never downloaded.
SPACY_MODEL = "en_core_web_sm"
LANGUAGE = "en"
# What Presidio looks for, and the spaCy labels that are each of them. Every other label of the
# model is ignored, so Presidio has nothing to warn about.
ENTITIES = ["PERSON", "LOCATION", "DATE_TIME"]
LABELS = {
    "PERSON": "PERSON",
    "GPE": "LOCATION",
    "LOC": "LOCATION",
    "FAC": "LOCATION",
    "DATE": "DATE_TIME",
}
IGNORED_LABELS = [
    "CARDINAL",
    "EVENT",
    "LANGUAGE",
    "LAW",
    "MONEY",
    "NORP",
    "ORDINAL",
    "ORG",
    "PERCENT",
    "PRODUCT",
    "QUANTITY",
    "TIME",
    "WORK_OF_ART",
]

# The tools a name is given to, and the argument that holds it.
NAME_ARGUMENTS = {
    "verify_identity": "full_name",
    "collect_new_patient": "full_name",
    "take_message": "name",
}

# One thread makes every record, one at a time: off the event loop, and the model is never used
# from two threads at once.
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="call-record")
# The records still being made: held until each has ended.
_PENDING: set[asyncio.Task[None]] = set()


def _load(model: str) -> tuple["AnalyzerEngine", "AnonymizerEngine"]:
    """Presidio's engines over the installed spaCy `model`. A model that is not installed is
    refused before Presidio could download it."""
    if importlib.util.find_spec(model) is None:
        raise RuntimeError("the spaCy model is not installed")
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NerModelConfiguration, SpacyNlpEngine
    from presidio_analyzer.predefined_recognizers import SpacyRecognizer
    from presidio_anonymizer import AnonymizerEngine

    quiet_presidio()
    nlp_engine = SpacyNlpEngine(
        models=[{"lang_code": LANGUAGE, "model_name": model}],
        ner_model_configuration=NerModelConfiguration(
            model_to_presidio_entity_mapping=LABELS, labels_to_ignore=IGNORED_LABELS
        ),
    )
    nlp_engine.load()
    registry = RecognizerRegistry(
        recognizers=[SpacyRecognizer(supported_language=LANGUAGE, supported_entities=ENTITIES)],
        supported_languages=[LANGUAGE],
    )
    analyzer = AnalyzerEngine(
        registry=registry, nlp_engine=nlp_engine, supported_languages=[LANGUAGE]
    )
    return analyzer, AnonymizerEngine()


@functools.cache
def _engines() -> tuple["AnalyzerEngine", "AnonymizerEngine"]:
    """The engines, loaded on first use and kept; a load that fails is tried again next time."""
    return _load(SPACY_MODEL)


def _scrub(text: str) -> str:
    """The text with the names, locations and dates Presidio finds replaced by their kind."""
    from presidio_anonymizer import RecognizerResult

    analyzer, anonymizer = _engines()
    found = [
        RecognizerResult(result.entity_type, result.start, result.end, result.score)
        for result in analyzer.analyze(text=text, language=LANGUAGE, entities=ENTITIES)
    ]
    return anonymizer.anonymize(text=text, analyzer_results=found).text


def _tool_names(history: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every name given to a tool in the conversation."""
    names = []
    for message in history:
        content = message["content"]
        if message["role"] != "assistant" or isinstance(content, str):
            continue
        for block in content:
            if block["type"] != "tool_use":
                continue
            argument = NAME_ARGUMENTS.get(block["name"])
            arguments = block["input"]
            if argument is not None and isinstance(arguments, Mapping):
                value = arguments.get(argument)
                if isinstance(value, str):
                    names.append(value)
    return names


def _transcript(history: Sequence[Mapping[str, Any]]) -> tuple[str, int]:
    """One line for each caller turn and each thing the agent said, in order, and the number of
    caller turns. Text the model wrote beside a tool call was never said and is left out."""
    lines: list[str] = []
    turns = 0
    for message in history:
        content = message["content"]
        if message["role"] == "user":
            if isinstance(content, str):
                lines.append(f"caller: {' '.join(content.splitlines())}")
                turns += 1
            continue
        if isinstance(content, str):
            said = [content]
        elif any(block["type"] == "tool_use" for block in content):
            said = []
        else:
            said = [block["text"] for block in content if block["type"] == "text"]
        lines += [f"agent: {' '.join(text.splitlines())}" for text in said if text.strip()]
    return "\n".join(lines), turns


def _cut(text: str) -> tuple[str, bool]:
    """The text, or as many of its first lines as fit in MAX_TRANSCRIPT characters."""
    if len(text) <= MAX_TRANSCRIPT:
        return text, False
    end = text.rfind("\n", 0, MAX_TRANSCRIPT + 1)
    return text[: max(end, 0)], True


def _masked(history: Sequence[Mapping[str, Any]], names: Sequence[str]) -> tuple[str, int, bool]:
    """The transcript cut to fit, passed through `mask` and with the names the call knows (the
    patient's `names` and those given to its tools) hidden; the number of caller turns; and
    whether it was cut."""
    text, turns = _transcript(history)
    text, cut = _cut(text)
    return hide_names(mask(text), [*names, *_tool_names(history)]), turns, cut


async def _patient_names(session: AsyncSession, patient_id: str) -> list[str]:
    """The first and last name of the patients row; a missing row raises."""
    statement = select(Patient.first_name, Patient.last_name).where(Patient.id == patient_id)
    first_name, last_name = (await session.execute(statement)).one()
    return [first_name, last_name]


async def _verified_names(state: CallState, deps: Deps) -> list[str]:
    """The verified caller's names from their patients row; if that read fails, the first name
    the state holds, and the failure is logged."""
    if state.patient_id is None:
        return []
    try:
        async with deps.sessions() as session:
            return await _patient_names(session, state.patient_id)
    except Exception as error:
        logger.warning(
            "call_record_patient_lookup_failed call_id=%s error=%s",
            state.call_id,
            type(error).__name__,
        )
        return [] if state.patient_first_name is None else [state.patient_first_name]


async def _record(
    state: CallState,
    history: Sequence[Mapping[str, Any]],
    ended_at: datetime,
    reason: Reason,
    deps: Deps,
) -> None:
    loop = asyncio.get_running_loop()
    try:
        names = await _verified_names(state, deps)
        masked, turns, cut = await loop.run_in_executor(_WORKER, _masked, history, names)
        try:
            masked = await loop.run_in_executor(_WORKER, _scrub, masked)
            names_masked = True
        except Exception as error:
            names_masked = False
            logger.warning(
                "call_record_presidio_failed call_id=%s error=%s",
                state.call_id,
                type(error).__name__,
            )
        transcript, cut_again = _cut(masked)
        truncated = cut or cut_again
        async with deps.sessions() as session:
            result = await session.execute(
                insert(CallSession)
                .values(
                    call_id=state.call_id,
                    started_at=state.started_at,
                    ended_at=ended_at,
                    office_open=state.office_open,
                    verified=state.verified,
                    patient_id=state.patient_id,
                    end_reason=reason.value,
                    turn_count=turns,
                    transcript=transcript,
                    transcript_truncated=truncated,
                    names_masked=names_masked,
                )
                .on_conflict_do_nothing(index_elements=["call_id"])
                .returning(CallSession.id)
            )
            inserted = result.first() is not None
            await session.commit()
    except Exception as error:
        logger.error("call_record_failed call_id=%s error=%s", state.call_id, type(error).__name__)
        return
    if inserted:
        logger.info(
            "call_recorded call_id=%s turns=%d truncated=%s", state.call_id, turns, truncated
        )
    else:
        logger.info("call_record_duplicate call_id=%s", state.call_id)


def record_call(
    state: CallState, history: Sequence[Mapping[str, Any]], reason: Reason, deps: Deps
) -> None:
    """Store the record of the ended call, by a task of its own; the caller does not wait for
    it. A call that has not ended, or a reason that is not one a call ends for, is refused."""
    if state.phase is not Phase.ENDED:
        raise ValueError("a call is recorded only once it has ended")
    if not isinstance(reason, Reason) or reason not in END_PHASES:
        raise ValueError("a call ends only for one of the fixed reasons")
    task = asyncio.create_task(_record(state, tuple(history), deps.now(), reason, deps))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def drain_call_records() -> None:
    """Wait until every record still being made has been stored or dropped."""
    while _PENDING:
        await asyncio.wait(set(_PENDING))
