"""Call the running app as a WebRTC client, send a tone, and check the same tone comes back.

Uses aiortc, the WebRTC library the pipecat-ai[webrtc] extra installs. Each call sends a tone
at a frequency chosen at random for that run and no other call, so a call that hears another
call's audio, or a recording, fails. A call that ends before the requested length fails too.

    uv run python scripts/echo_probe.py --url http://127.0.0.1:8000 --calls 2 [--relay]

The access token is read from the PROBE_TOKEN environment variable. Exit code 0 means
every call heard its own tone back.
"""

import argparse
import asyncio
import fractions
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

import numpy as np
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCIceTransportPolicy,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame

SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = 960  # 20 ms
# Candidate tones 60 Hz apart, so each call's tone is far from every other call's.
TONES_HZ = [float(hz) for hz in range(300, 1500, 60)]
MAX_CALLS = 4
SKIP_FRAMES = 25  # the first half second while the jitter buffers fill
MIN_RMS = 500.0  # full-scale int16 is 32767; the tone is sent at half scale
MAX_PEAK_ERROR_HZ = 10.0


class ToneTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, frequency: float) -> None:
        super().__init__()
        self._frequency = frequency
        self._sent = 0
        self._start: float | None = None

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        if self._start is None:
            self._start = time.monotonic()
        delay = self._start + self._sent / SAMPLE_RATE - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        index = np.arange(self._sent, self._sent + SAMPLES_PER_FRAME)
        wave = 0.5 * 32767 * np.sin(2 * np.pi * self._frequency * index / SAMPLE_RATE)
        frame = AudioFrame.from_ndarray(
            wave.astype(np.int16).reshape(1, -1), format="s16", layout="mono"
        )
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._sent
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._sent += SAMPLES_PER_FRAME
        return frame


def http(method: str, url: str, token: str | None, body: dict[str, str] | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def candidate_types(sdp: str) -> dict[str, int]:
    types = Counter(line.split()[7] for line in sdp.splitlines() if line.startswith("a=candidate:"))
    return dict(types)


def dominant_frequency(samples: np.ndarray, sample_rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    frequencies = np.fft.rfftfreq(len(samples), 1 / sample_rate)
    return float(frequencies[int(np.argmax(spectrum[1:])) + 1])


async def one_call(
    url: str, token: str | None, frequency: float, relay: bool, seconds: float
) -> bool:
    label = f"call {frequency:.0f} Hz"
    ice = await asyncio.to_thread(http, "GET", f"{url}/ice-servers", token)
    servers = [
        RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
        for s in ice["iceServers"]
    ]
    policy = RTCIceTransportPolicy.RELAY if relay else RTCIceTransportPolicy.ALL
    pc = RTCPeerConnection(RTCConfiguration(iceServers=servers, iceTransportPolicy=policy))
    received: list[np.ndarray] = []
    sample_rate = SAMPLE_RATE
    enough = asyncio.Event()
    wanted = SKIP_FRAMES + int(seconds * 50)

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        async def read() -> None:
            nonlocal sample_rate
            while len(received) < wanted:
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    break
                if not isinstance(frame, AudioFrame):
                    break
                sample_rate = frame.sample_rate
                channels = len(frame.layout.channels)
                received.append(frame.to_ndarray().reshape(-1, channels)[:, 0])
            enough.set()

        asyncio.ensure_future(read())

    try:
        pc.addTransceiver(ToneTrack(frequency), direction="sendrecv")
        await pc.setLocalDescription(await pc.createOffer())
        print(f"{label}: offer candidates {candidate_types(pc.localDescription.sdp)}")
        started = time.monotonic()
        answer = await asyncio.to_thread(
            http,
            "POST",
            f"{url}/offer",
            token,
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
        )
        print(f"{label}: answer candidates {candidate_types(answer['sdp'])}")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))
        await asyncio.wait_for(enough.wait(), timeout=seconds + 30)
        print(f"{label}: ice {pc.iceConnectionState}, {time.monotonic() - started:.1f}s to audio")
    except (TimeoutError, urllib.error.URLError) as error:
        print(f"{label}: FAILED {type(error).__name__} {error}; {len(received)} frames received")
        return False
    finally:
        await pc.close()

    if len(received) < wanted:
        print(f"{label}: FAILED the call ended after {len(received)} of {wanted} frames")
        return False
    samples = np.concatenate(received[SKIP_FRAMES:]).astype(np.float64)
    rms = float(np.sqrt(np.mean(samples**2)))
    peak = dominant_frequency(samples, sample_rate)
    ok = rms >= MIN_RMS and abs(peak - frequency) <= MAX_PEAK_ERROR_HZ
    verdict = "OK" if ok else "FAILED"
    print(f"{label}: {verdict} {len(samples)} samples back, rms {rms:.0f}, peak {peak:.1f} Hz")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--calls", type=int, default=1, choices=range(1, MAX_CALLS + 1))
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--relay", action="store_true", help="use TURN relay candidates only")
    args = parser.parse_args()
    token = os.environ.get("PROBE_TOKEN")

    results = await asyncio.gather(
        *(
            one_call(args.url.rstrip("/"), token, tone, args.relay, args.seconds)
            for tone in random.SystemRandom().sample(TONES_HZ, args.calls)
        )
    )
    print(f"{sum(results)}/{len(results)} calls heard their own tone back")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
