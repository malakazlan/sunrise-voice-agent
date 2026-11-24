"""Call the running app as a WebRTC client, send a tone, and check that the server received it.

Uses aiortc, the WebRTC library the pipecat-ai[webrtc] extra installs. The proof is the
server's own RTCP receiver reports for this call's stream, whatever it sends back: they say
how many of the call's packets arrived, and they come back over the same path, so with --relay
both directions went through TURN. A report is for one stream (its SSRC), so a call never
counts another call's reports. A call whose reports do not show the requested length of audio
fails.

    uv run python scripts/relay_probe.py --url http://127.0.0.1:8000 --calls 2 [--relay]

With --hoard N it first opens N relay-only connections on one credential, as a greedy client
would, and holds them while the calls run; --expect-hoarded M fails the run unless exactly M of
them got a relay (coturn's per-credential quota).

The access token is read from the PROBE_TOKEN environment variable. Exit code 0 means the
server reported every call's audio received.
"""

import argparse
import asyncio
import fractions
import json
import os
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
from aiortc.stats import RTCRemoteInboundRtpStreamStats
from av import AudioFrame

SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = 960  # 20 ms
TONE_HZ = 440.0
PACKETS_PER_SECOND = 50  # one per 20 ms frame
POLL_SECONDS = 0.2
MAX_CALLS = 4
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


async def packets_received(pc: RTCPeerConnection) -> int:
    """How many of this call's audio packets the server says it received, from its latest
    receiver report; 0 before the first one."""
    for report in (await pc.getStats()).values():
        if isinstance(report, RTCRemoteInboundRtpStreamStats) and report.kind == "audio":
            return report.packetsReceived
    return 0


async def one_call(url: str, token: str | None, number: int, relay: bool, seconds: float) -> bool:
    label = f"call {number}"
    ice = await asyncio.to_thread(http, "GET", f"{url}/ice-servers", token)
    servers = [
        RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
        for s in ice["iceServers"]
    ]
    policy = RTCIceTransportPolicy.RELAY if relay else RTCIceTransportPolicy.ALL
    pc = RTCPeerConnection(RTCConfiguration(iceServers=servers, iceTransportPolicy=policy))
    wanted = int(seconds * PACKETS_PER_SECOND)
    received = 0

    async def reported() -> None:
        nonlocal received
        while received < wanted:
            await asyncio.sleep(POLL_SECONDS)
            received = await packets_received(pc)

    waiting_for = "gathering candidates"
    try:
        pc.addTransceiver(ToneTrack(TONE_HZ), direction="sendrecv")
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
        waiting_for = f"the server to report {wanted} packets received"
        await bounded(reported(), seconds + 30)
        elapsed = time.monotonic() - started
    except TimeoutError:
        print(f"{label}: FAILED timed out waiting for {waiting_for}; {received} reported")
        return False
    except urllib.error.URLError as error:
        print(f"{label}: FAILED {type(error).__name__} {error} while waiting for {waiting_for}")
        return False
    finally:
        await close(pc, label)

    print(f"{label}: OK ice {pc.iceConnectionState}, {received} packets received by the server,")
    print(f"{label}: reported {elapsed:.1f}s after the offer")
    return True


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
                one_call(url, token, number, args.relay, args.seconds)
                for number in range(args.calls)
            )
        )
    finally:
        await asyncio.gather(*(close(pc, "hoarding connection") for pc in hoarded))
    print(f"{sum(results)}/{len(results)} calls had their audio received by the server")
    return 0 if all(results) and hoard_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
