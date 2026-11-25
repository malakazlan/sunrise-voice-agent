import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.call_record import drain_call_records
from app.agent.claims import ClaimKeys
from app.agent.endings import drain_call_events
from app.agent.model import anthropic_model
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import drain_guard_audits
from app.agent.turn_record import TurnRecorder
from app.api.offer import CallRefusals, OpenCall, build_router
from app.api.service import mount_admin
from app.audit.log import AuditLog
from app.audit.queue import AuditQueue
from app.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.domain.office_hours import clinic_now
from app.guard.logging import configure_logging
from app.runtime_config import ConfigPoll, LiveConfig
from app.voice.calls import CallRegistry
from app.voice.pipeline import END_GRACE_SECONDS, open_agent_call
from app.voice.speech_check import SpeechCheck
from app.voice.vocabulary import Vocabulary

logger = logging.getLogger(__name__)

CALL_PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"
# How long shutdown waits, in all, for the turn rows and the refused calls' events still
# queued: both are drained at once under this one limit, and what is left then is dropped, so a
# stalled database cannot hold the stop past the container's grace period.
QUEUES_WAIT_SECONDS = 5
# The loop's default executor runs what a call hands it: aiortc's encoding of each audio frame
# it sends, one at a time for the call's one track, and at set-up one load or lookup at a time
# (the VAD's model, the STUN server's address); so two workers per call at once. Decoding runs
# on aiortc's own thread for the call. Address lookups of the database's host share it, and
# can take seconds: hence the spare workers. How long the workers may take to start, at
# start-up.
WORKERS_PER_CALL = 2
SPARE_WORKERS = 2
WORKERS_START_SECONDS = 10


def worker_count(cpus: int | None, calls: int) -> int:
    """How many workers the loop's default executor gets: never fewer than Python's own
    default, min(32, cpus + 4), and enough for every call at once with some to spare."""
    return max(min(32, (cpus or 1) + 4), WORKERS_PER_CALL * calls + SPARE_WORKERS)


async def start_workers(count: int) -> None:
    """Give the running loop a default executor of `count` workers, every one started now. A
    worker started later would start on the event loop's thread, which waits for it, and every
    call's audio with it. Each worker waits until all have started, so each is its own; a
    worker that does not start in WORKERS_START_SECONDS stops the start-up."""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=count, thread_name_prefix="call_worker")
    loop.set_default_executor(executor)
    together = threading.Barrier(count)
    await asyncio.gather(
        *(
            loop.run_in_executor(executor, together.wait, WORKERS_START_SECONDS)
            for _ in range(count)
        )
    )
    logger.info("call_workers_started workers=%d", count)


def build_deps(settings: Settings, engine: AsyncEngine, config: LiveConfig) -> Deps:
    """What tool dispatch needs, built once from the settings: sessions as the runtime role,
    the audit log, the clinic clock (simulated in dev and demo when set), the claim hasher and
    the config in memory."""
    if settings.identity_hash_key is None:
        raise ValueError("the calls need identity_hash_key")
    return Deps(
        sessions=create_session_factory(engine),
        audit=AuditLog(settings.audit_hmac_key),
        now=partial(clinic_now, settings),
        claim_keys=ClaimKeys(settings.identity_hash_key),
        config=config,
    )


def create_app(
    settings: Settings | None = None,
    open_call: OpenCall | None = None,
    config: LiveConfig | None = None,
) -> FastAPI:
    """The calls' application. `config` is the config in memory to start from: none read yet
    unless given; the poll keeps it to the table either way."""
    if settings is None:
        settings = Settings()
    # The admin role never loads the calls: app/server.py builds it without this module.
    if settings.app_role == "admin":
        raise ValueError("the admin role is served by app.server, not the calls' application")

    configure_logging(settings)
    register_tools()

    engine = create_database_engine(settings)
    if config is None:
        config = LiveConfig()
    deps = build_deps(settings, engine, config)
    # The refused calls' events, sparingly, written in the background.
    refused_events = AuditQueue(deps.sessions, deps.audit, deps.now)
    refusals = CallRefusals(refused_events)
    model = anthropic_model(settings)
    vocabulary = Vocabulary()
    speech_check = SpeechCheck(settings)
    # Every call's turn rows, written in the background with the call's clock.
    turns = TurnRecorder(deps.sessions, deps.now)
    if open_call is None:
        open_call = partial(
            open_agent_call, settings, deps, model, vocabulary, speech_check, turns=turns
        )
    # A call says goodbye and ends itself at MAX_CALL_SECONDS; the registry stops one that has
    # not, END_GRACE_SECONDS later.
    calls = CallRegistry(
        max_calls=settings.max_concurrent_calls,
        max_seconds=settings.max_call_seconds + END_GRACE_SECONDS,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Speech-to-text's hints, read once before the first call; and the config in memory,
        # read now and then every few seconds by one task, which ends with the lifespan.
        await vocabulary.load(deps.sessions)
        poll = ConfigPoll(deps.sessions, config)
        _app.state.poll = poll
        await poll.start()
        # Before the first call, never during one.
        await start_workers(worker_count(os.cpu_count(), settings.max_concurrent_calls))
        yield
        await poll.stop()
        refusals.close()
        await calls.close()
        await speech_check.drain()
        # The call events and guard blocks still being written (each bounded) end before the
        # engine goes.
        await drain_call_events()
        await drain_guard_audits()
        # And the call records still being masked and stored; then the turn rows and the
        # refused calls' events still queued, together, under one limit.
        await drain_call_records()
        await asyncio.gather(
            turns.drain(timeout=QUEUES_WAIT_SECONDS),
            refused_events.drain(timeout=QUEUES_WAIT_SECONDS),
        )
        if model is not None:
            await model.close()
        await engine.dispose()

    docs_enabled = settings.app_env == "dev"
    app = FastAPI(
        title="Sunrise voice agent",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.calls = calls
    app.state.deps = deps
    app.state.vocabulary = vocabulary
    app.state.turns = turns
    app.state.config = config
    page = CALL_PAGE.read_bytes()

    # Coroutines, so that nothing here runs in the thread pool, whose new threads hold the
    # event loop while they start (app/api/offer.py); the page is read once, here.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def call_page() -> Response:
        return Response(page, media_type="text/html")

    app.include_router(build_router(settings, calls, open_call, config, refusals))

    # The voice role serves no admin page, whatever it is given.
    if settings.app_role == "voice" or settings.admin_password is None:
        logger.warning("admin_api_off")
    else:
        mount_admin(app, settings, deps.sessions, deps.audit)

    return app
