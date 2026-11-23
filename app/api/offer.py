"""Signaling: the ICE-server list and the offer/answer exchange, both behind the access token."""

import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.config import Settings
from app.voice.calls import Call, CallRegistry
from app.voice.turn import browser_ice_servers

logger = logging.getLogger(__name__)

# (call_id, sdp, sdp_type) -> a call ready to run, with its answer
OpenCall = Callable[[str, str, str], Awaitable[Call]]
MAX_SDP_LENGTH = 20_000


class Offer(BaseModel):
    sdp: str = Field(min_length=1, max_length=MAX_SDP_LENGTH)
    type: Literal["offer"]


def _token_matches(request: Request, settings: Settings) -> bool:
    expected = settings.demo_access_token
    if expected is None:
        return True
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme != "Bearer":
        return False
    return hmac.compare_digest(supplied.encode(), expected.get_secret_value().encode())


def build_router(settings: Settings, calls: CallRegistry, open_call: OpenCall) -> APIRouter:
    def require_token(request: Request) -> None:
        # A dependency, so it runs before the request body is even validated.
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

    @router.post("/offer")
    async def offer(body: Offer) -> dict[str, str]:
        call_id = calls.reserve()
        if call_id is None:
            logger.info("call_refused reason=cap active=%d", calls.active)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Too many calls")
        try:
            call = await open_call(call_id, body.sdp, body.type)
        except Exception as error:
            calls.release(call_id)
            logger.info(
                "call_setup_failed call_id=%s error=%s active=%d",
                call_id,
                type(error).__name__,
                calls.active,
            )
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Call setup failed") from None
        calls.start(call_id, call)
        return {**call.answer(), "call_id": call_id}

    return router
