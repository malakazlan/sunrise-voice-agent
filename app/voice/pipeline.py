"""The echo call: the caller's audio comes straight back to them."""

import asyncio
import logging
import time

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from app.config import Settings
from app.voice.transport import audio_transport, open_connection, server_ice_servers

logger = logging.getLogger(__name__)

# A call whose media path is not up by then is ended and its slot given back.
CONNECT_TIMEOUT_SECONDS = 30.0


class EchoProcessor(FrameProcessor):
    """Turns each chunk of caller audio into a chunk of output audio."""

    def __init__(self) -> None:
        super().__init__()
        self.echoed_frames = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.echoed_frames += 1
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=frame.audio,
                    sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                )
            )
        else:
            await self.push_frame(frame, direction)


class EchoCall:
    def __init__(
        self, call_id: str, connection: SmallWebRTCConnection, connect_timeout: float
    ) -> None:
        self._call_id = call_id
        self._connection = connection
        self._connect_timeout = connect_timeout
        self._connected = asyncio.Event()
        self._stopped = False
        self._echo = EchoProcessor()
        transport = audio_transport(connection)
        pipeline = Pipeline([transport.input(), self._echo, transport.output()])
        # No RTVI (the call page has no data channel) and no idle timeout: the call
        # registry owns the call's lifetime.
        self._worker = PipelineWorker(pipeline, enable_rtvi=False, idle_timeout_secs=None)
        self._runner = WorkerRunner(handle_sigint=False)
        transport.add_event_handler("on_client_connected", self._on_client_connected)
        transport.add_event_handler("on_client_disconnected", self._on_client_disconnected)

    async def _on_client_connected(
        self, transport: SmallWebRTCTransport, client: SmallWebRTCConnection
    ) -> None:
        logger.info("call_connected call_id=%s", self._call_id)
        self._connected.set()

    async def _on_client_disconnected(
        self, transport: SmallWebRTCTransport, client: SmallWebRTCConnection
    ) -> None:
        logger.info("call_disconnected call_id=%s", self._call_id)
        await self._runner.cancel()

    def answer(self) -> dict[str, str]:
        answer = self._connection.get_answer()
        return {"sdp": answer["sdp"], "type": answer["type"]}

    async def run(self) -> None:
        await self._runner.add_workers(self._worker)
        # run() clears the runner's cancel request, so a stop that came first must win here.
        if self._stopped:
            return
        running = asyncio.ensure_future(self._runner.run())
        connected = asyncio.ensure_future(self._connected.wait())
        try:
            done, _pending = await asyncio.wait(
                {running, connected},
                timeout=self._connect_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                logger.info("call_not_connected call_id=%s", self._call_id)
                await self._runner.cancel()
            await running
        finally:
            connected.cancel()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        await self._runner.cancel()
        await self._connection.disconnect()
        logger.info(
            "call_audio call_id=%s frames_echoed=%d", self._call_id, self._echo.echoed_frames
        )


async def open_echo_call(
    settings: Settings,
    call_id: str,
    sdp: str,
    sdp_type: str,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
) -> EchoCall:
    ice_servers = server_ice_servers(settings, now=time.time())
    # Pipecat's own connect timeout closes the peer connection from inside the task it then
    # cancels (pipecat-ai 1.17.0), leaving the close unfinished when TURN is in use, so every
    # later close waits forever. It is set past the longest call so that it never fires; this
    # call ends itself after connect_timeout instead.
    connection = await open_connection(
        sdp, sdp_type, ice_servers, connection_timeout_secs=2 * settings.max_call_seconds
    )
    try:
        return EchoCall(call_id, connection, connect_timeout)
    except BaseException:
        await connection.disconnect()
        raise
