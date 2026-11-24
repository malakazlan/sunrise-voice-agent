"""Why speech out refused. Pipecat's ElevenLabs service (pipecat-ai 1.17.0) does not read the
error messages ElevenLabs sends on its WebSocket: a voice the plan does not allow shows only as
lines with no audio, and a voice that does not exist as a close. So once a call has ended with
`error` because speech out could no longer work, one request goes to ElevenLabs' plain
text-to-speech endpoint (POST /v1/text-to-speech/{voice_id}, `xi-api-key`, JSON `text` and
`model_id`) with the configured voice and model and two words, and its answer is logged in one
line: `speech_refused status=<HTTP status> code=<ElevenLabs' error code>`, `speech_check_ok`, or
`speech_check_failed error=<exception class>`. ElevenLabs answers an error with a JSON body whose
`detail.code` names it; only a code of KNOWN_CODES is logged, any other answer as `other`.
Never the key, the voice id, the vendor's message or the request.

The request runs in a task of its own that nobody waits for, given up after
CHECK_DEADLINE_SECONDS; at most one is made per CHECK_INTERVAL_SECONDS, whatever the calls.
The application holds one SpeechCheck (app/main.py).
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable

from httpx2 import AsyncBaseTransport, AsyncClient

from app.config import Settings

logger = logging.getLogger(__name__)

ELEVENLABS_API = "https://api.elevenlabs.io"
CHECK_TEXT = "Speech check."
CHECK_DEADLINE_SECONDS = 5.0
CHECK_INTERVAL_SECONDS = 60.0
OTHER = "other"
# ElevenLabs' error codes that say why speech out would refuse (its API errors page), and
# `paid_plan_required`, its answer for a voice the plan does not allow, which that page omits.
KNOWN_CODES = frozenset(
    {
        "voice_not_found",
        "model_not_found",
        "invalid_voice_id",
        "unsupported_model",
        "invalid_api_key",
        "missing_api_key",
        "unauthorized",
        "insufficient_credits",
        "paid_plan_required",
        "subscription_required",
        "feature_not_available",
        "voice_access_denied",
        "model_access_denied",
        "forbidden",
        "insufficient_permissions",
        "rate_limit_exceeded",
        "concurrent_limit_exceeded",
        "system_busy",
        "internal_error",
        "service_unavailable",
    }
)


def error_code(body: bytes) -> str:
    """ElevenLabs' error code in an answer's body when it is one of KNOWN_CODES; OTHER for
    anything else."""
    try:
        data = json.loads(body)
    except ValueError:
        return OTHER
    detail = data.get("detail") if isinstance(data, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) and code in KNOWN_CODES else OTHER


class SpeechCheck:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: AsyncBaseTransport | None = None,
        deadline: float = CHECK_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._deadline = deadline
        self._clock = clock
        self._last: float | None = None
        self._running: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        """Ask ElevenLabs why, in the background, unless it was asked within the last
        CHECK_INTERVAL_SECONDS. Returns at once."""
        now = self._clock()
        if self._last is not None and now - self._last < CHECK_INTERVAL_SECONDS:
            return
        self._last = now
        task = asyncio.ensure_future(self._bounded())
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def drain(self) -> None:
        """Wait for the checks still running: for tests and for shutdown."""
        while self._running:
            await asyncio.wait(set(self._running))

    async def _bounded(self) -> None:
        try:
            async with asyncio.timeout(self._deadline):
                await self._check()
        except Exception as error:
            logger.warning("speech_check_failed error=%s", type(error).__name__)

    async def _check(self) -> None:
        key = self._settings.elevenlabs_api_key
        voice = self._settings.elevenlabs_voice_id
        if key is None or voice is None:
            raise ValueError("the check needs the ElevenLabs key and voice")
        async with AsyncClient(
            base_url=ELEVENLABS_API, transport=self._transport, timeout=self._deadline
        ) as client:
            answer = await client.post(
                f"/v1/text-to-speech/{voice}",
                headers={"xi-api-key": key.get_secret_value()},
                json={"text": CHECK_TEXT, "model_id": self._settings.tts_model},
            )
        if answer.is_success:
            logger.info("speech_check_ok")
            return
        logger.warning(
            "speech_refused status=%d code=%s", answer.status_code, error_code(answer.content)
        )
