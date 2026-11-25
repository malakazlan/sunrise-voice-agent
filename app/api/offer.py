"""Signaling: the ICE-server list and the offer/answer exchange, both behind the access token.

A new call is taken only while the config in memory says calls are on (app/runtime_config.py):
with calls off, or before the config has been read, an offer is refused with a fixed answer
(UNAVAILABLE), before its body is read, and the refusal is audited by a background writer
(app/audit/queue.py) that the answer does not wait for. Calls already connected are not
touched."""

import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.audit.log import AGENT, Action, Event, Outcome, Reason, ResourceType
from app.audit.queue import AuditQueue
from app.config import Settings
from app.runtime_config import LiveConfig
from app.voice.calls import Call, CallRegistry
from app.voice.turn import browser_ice_servers

logger = logging.getLogger(__name__)

# (call_id, sdp, sdp_type) -> a call ready to run, with its answer
OpenCall = Callable[[str, str, str], Awaitable[Call]]
MAX_SDP_LENGTH = 20_000
# A browser's offer with all its candidates is a few kilobytes.
MAX_BODY_BYTES = 32 * 1024
# The answer to an offer while no call is taken; the call page (web/index.html) words it for
# the caller.
UNAVAILABLE = {"reason": "unavailable"}


class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdp: str = Field(min_length=1, max_length=MAX_SDP_LENGTH)
    type: Literal["offer"]


def _token_matches(request: Request, settings: Settings) -> bool:
    expected = settings.demo_access_token
    if expected is None:
        return True
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    supplied = supplied.strip()
    if scheme != "Bearer" or not supplied:
        return False
    return hmac.compare_digest(supplied.encode(), expected.get_secret_value().encode())


def _too_large() -> HTTPException:
    return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="Offer too large")


async def _read_offer(request: Request) -> Offer:
    """Read at most MAX_BODY_BYTES of the body, then parse it."""
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length")
        if int(declared) > MAX_BODY_BYTES:
            raise _too_large()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise _too_large()
    try:
        return Offer.model_validate_json(bytes(body))
    except ValidationError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid offer") from None


def _refused(reason: Reason) -> Event:
    """A call refused before it began: no call id, nothing of the caller."""
    return Event(
        actor=AGENT,
        action=Action.CALL_REFUSED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.REFUSED,
        reason=reason,
    )


def build_router(
    settings: Settings,
    calls: CallRegistry,
    open_call: OpenCall,
    config: LiveConfig,
    refusals: AuditQueue,
) -> APIRouter:
    def require_token(request: Request) -> None:
        # A dependency of every route here, and no route declares a body, so the token is
        # checked before any of the body is read.
        if not _token_matches(request, settings):
            logger.info("request_refused path=%s reason=token", request.url.path)
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="Access token required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    router = APIRouter(dependencies=[Depends(require_token)])

    @router.get("/ice-servers")
    def ice_servers(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return {"iceServers": browser_ice_servers(settings, now=time.time())}

    @router.post("/offer", response_model=None)
    async def offer(request: Request) -> dict[str, str] | JSONResponse:
        if not config.takes_calls():
            reason = Reason.CONFIG_UNKNOWN if config.values is None else Reason.CALLS_OFF
            refusals.put(_refused(reason))
            logger.info("call_refused reason=%s", reason.value)
            return JSONResponse(UNAVAILABLE, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        body = await _read_offer(request)
        call_id = calls.reserve()
        if call_id is None:
            logger.info("call_refused reason=cap active=%d", calls.active)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Too many calls")
        started = False
        try:
            try:
                call = await open_call(call_id, body.sdp, body.type)
            except Exception as error:
                logger.info("call_setup_failed call_id=%s error=%s", call_id, type(error).__name__)
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, detail="Call setup failed"
                ) from None
            calls.start(call_id, call)
            started = True
        finally:
            # Also reached when the request is cancelled during setup.
            if not started:
                calls.release(call_id)
        return {**call.answer(), "call_id": call_id}

    return router
