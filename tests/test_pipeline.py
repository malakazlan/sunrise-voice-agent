import asyncio
from typing import Any

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection
from aiortc.mediastreams import AudioStreamTrack
from conftest import offline_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, TextFrame
from pipecat.tests.utils import run_test
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

import app.voice.pipeline
import app.voice.transport
from app.main import create_app
from app.voice.calls import CallRegistry
from app.voice.pipeline import EchoProcessor, open_echo_call

CHUNK = bytes(range(256)) * 2


def test_caller_audio_is_sent_back_unchanged() -> None:
    frames = [
        InputAudioRawFrame(audio=CHUNK, sample_rate=16000, num_channels=1),
        TextFrame(text="passes through"),
    ]

    down, _up = asyncio.run(
        run_test(
            EchoProcessor(),
            frames_to_send=frames,
            expected_down_frames=[OutputAudioRawFrame, TextFrame],
        )
    )

    echoed = down[0]
    assert isinstance(echoed, OutputAudioRawFrame)
    assert (echoed.audio, echoed.sample_rate, echoed.num_channels) == (CHUNK, 16000, 1)
    assert isinstance(down[1], TextFrame)
    assert down[1].text == "passes through"


def test_call_that_never_connects_ends_itself() -> None:
    # A real offer with its candidates removed: the server can never reach the caller.
    # Only local sockets are opened; no ICE servers are configured.
    async def scenario() -> None:
        caller = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        try:
            caller.addTransceiver(AudioStreamTrack(), direction="sendrecv")
            await caller.setLocalDescription(await caller.createOffer())
            lines = caller.localDescription.sdp.splitlines()
            offer = "\r\n".join(line for line in lines if not line.startswith("a=candidate:"))

            call = await open_echo_call(
                offline_settings(
                    app_env="dev", demo_access_token=None, turn_host=None, turn_shared_secret=None
                ),
                "test-call",
                offer + "\r\n",
                "offer",
                connect_timeout=0.5,
            )
            # asyncio.wait, not wait_for: Pipecat's runner swallows cancellation, so a
            # cancelled run() would look like one that ended by itself.
            running = asyncio.ensure_future(call.run())
            ended, _ = await asyncio.wait({running}, timeout=15)
            stopping = asyncio.ensure_future(call.stop())
            stopped, _ = await asyncio.wait({stopping, running}, timeout=15)

            assert running in ended, "a call that never connects must end itself"
            assert stopping in stopped
        finally:
            await caller.close()

    asyncio.run(scenario())


DEV_WITHOUT_VOICE: dict[str, Any] = {
    "app_env": "dev",
    "demo_access_token": None,
    "turn_host": None,
    "turn_shared_secret": None,
}


def record_connections(monkeypatch: pytest.MonkeyPatch) -> list[SmallWebRTCConnection]:
    """Every peer connection the app opens, so a test can see whether it was closed."""
    opened: list[SmallWebRTCConnection] = []

    class Recorded(SmallWebRTCConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            opened.append(self)

    monkeypatch.setattr(app.voice.transport, "SmallWebRTCConnection", Recorded)
    return opened


def active_calls(fastapi_app: FastAPI) -> int:
    calls: CallRegistry = fastapi_app.state.calls
    return calls.active


async def offer_without_candidates() -> str:
    caller = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        caller.addTransceiver(AudioStreamTrack(), direction="sendrecv")
        await caller.setLocalDescription(await caller.createOffer())
        lines = caller.localDescription.sdp.splitlines()
        return "\r\n".join(line for line in lines if not line.startswith("a=candidate:")) + "\r\n"
    finally:
        await caller.close()


def test_connection_closed_when_answer_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # The peer connection exists, then answering the offer fails on a malformed SDP.
    opened = record_connections(monkeypatch)
    fastapi_app = create_app(offline_settings(**DEV_WITHOUT_VOICE))

    with TestClient(fastapi_app) as client:
        response = client.post("/offer", json={"sdp": "v=0 not-an-sdp", "type": "offer"})

        assert response.status_code == 400
        assert active_calls(fastapi_app) == 0

    assert len(opened) == 1
    assert opened[0].pc.connectionState == "closed"


def test_connection_closed_when_call_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # The offer is answered, then building the call around the connection fails.
    opened = record_connections(monkeypatch)

    def broken_call(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("call setup failed")

    monkeypatch.setattr(app.voice.pipeline, "EchoCall", broken_call)
    offer = asyncio.run(offer_without_candidates())
    fastapi_app = create_app(offline_settings(**DEV_WITHOUT_VOICE))

    with TestClient(fastapi_app) as client:
        response = client.post("/offer", json={"sdp": offer, "type": "offer"})

        assert response.status_code == 400
        assert active_calls(fastapi_app) == 0

    assert len(opened) == 1
    assert opened[0].pc.connectionState == "closed"
