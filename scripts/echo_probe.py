"""Call the running app as a WebRTC client, send a tone, and check the same tone comes back.

Uses aiortc, the WebRTC library the pipecat-ai[webrtc] extra installs. Each call sends a tone
at a frequency chosen at random for that run and no other call, so a call that hears another
call's audio, or a recording, fails. A call that ends before the requested length fails too.

    uv run python scripts/echo_probe.py --url http://127.0.0.1:8000 --calls 2 [--relay]

With --hoard N it first opens N relay-only connections on one credential, as a greedy client
would, and holds them while the calls run; --expect-hoarded M fails the run unless exactly M of
them got a relay (coturn's per-credential quota).

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
from collections.abc import Awaitable
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
# Bound for each signaling step (gathering, answering, closing); HTTP requests have their own.
STEP_TIMEOUT = 30.0


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


async def bounded[T](step: Awaitable[T], seconds: float) -> T:
    """Wait for one step of the call for at most `seconds` (raises TimeoutError)."""
    return await asyncio.wait_for(step, timeout=seconds)


async def close(pc: RTCPeerConnection, label: str) -> None:
    try:
        await bounded(pc.close(), STEP_TIMEOUT)
    except TimeoutError:
        print(f"{label}: closing the peer connection took over {STEP_TIMEOUT:.0f} s; left open")


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

    waiting_for = "gathering candidates"
    try:
        pc.addTransceiver(ToneTrack(frequency), direction="sendrecv")
        await bounded(pc.setLocalDescription(await pc.createOffer()), STEP_TIMEOUT)
        print(f"{label}: offer candidates {candidate_types(pc.localDescription.sdp)}")
        waiting_for = "the answer"
        started = time.monotonic()
        answer = await asyncio.to_thread(
            http,
            "POST",
            f"{url}/offer",
            token,
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
        )
        print(f"{label}: answer candidates {candidate_types(answer['sdp'])}")
        await bounded(
            pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer")),
            STEP_TIMEOUT,
        )
        waiting_for = f"{wanted} audio frames back"
        await bounded(enough.wait(), seconds + 30)
        print(f"{label}: ice {pc.iceConnectionState}, {time.monotonic() - started:.1f}s to audio")
    except TimeoutError:
        print(f"{label}: FAILED timed out waiting for {waiting_for}; {len(received)} frames back")
        return False
    except urllib.error.URLError as error:
        print(f"{label}: FAILED {type(error).__name__} {error} while waiting for {waiting_for}")
        return False
    finally:
        await close(pc, label)

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


async def hoard_relays(url: str, token: str | None, count: int) -> list[RTCPeerConnection]:
    """Open `count` relay-only peer connections on one credential, as a greedy client would.

    Each one asks coturn for an allocation while gathering; the ones refused by coturn's
    per-user quota end up with no relay candidate.
    """
    ice = await asyncio.to_thread(http, "GET", f"{url}/ice-servers", token)
    servers = [
        RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
        for s in ice["iceServers"]
    ]
    connections = [
        RTCPeerConnection(
            RTCConfiguration(iceServers=servers, iceTransportPolicy=RTCIceTransportPolicy.RELAY)
        )
        for _ in range(count)
    ]

    async def gather(pc: RTCPeerConnection) -> None:
        pc.addTransceiver("audio", direction="sendrecv")
        await bounded(pc.setLocalDescription(await pc.createOffer()), STEP_TIMEOUT)

    # A connection whose gathering failed or timed out simply has no relay candidate.
    await asyncio.gather(*(gather(pc) for pc in connections), return_exceptions=True)
    return connections


def relay_count(connections: list[RTCPeerConnection]) -> int:
    return sum(
        1
        for pc in connections
        if pc.localDescription is not None
        and candidate_types(pc.localDescription.sdp).get("relay", 0) > 0
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--calls", type=int, default=1, choices=range(1, MAX_CALLS + 1))
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--relay", action="store_true", help="use TURN relay candidates only")
    parser.add_argument(
        "--hoard",
        type=int,
        default=0,
        help="first open this many relay-only connections on one credential and hold them",
    )
    parser.add_argument(
        "--expect-hoarded",
        type=int,
        default=None,
        help="fail unless exactly this many of the hoarding connections got a relay",
    )
    args = parser.parse_args()
    url = args.url.rstrip("/")
    token = os.environ.get("PROBE_TOKEN")

    hoarded: list[RTCPeerConnection] = []
    try:
        hoard_ok = True
        if args.hoard:
            hoarded = await hoard_relays(url, token, args.hoard)
            relays = relay_count(hoarded)
            print(f"hoarded relays: {relays} of {args.hoard} connections on one credential")
            hoard_ok = args.expect_hoarded is None or relays == args.expect_hoarded
        results = await asyncio.gather(
            *(
                one_call(url, token, tone, args.relay, args.seconds)
                for tone in random.SystemRandom().sample(TONES_HZ, args.calls)
            )
        )
    finally:
        await asyncio.gather(*(close(pc, "hoarding connection") for pc in hoarded))
    print(f"{sum(results)}/{len(results)} calls heard their own tone back")
    return 0 if all(results) and hoard_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
