"""The server end of a call: a SmallWebRTC peer connection and the transport around it."""

from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app.config import Settings
from app.voice.turn import issue_credential, turn_urls


def server_ice_servers(settings: Settings, now: float) -> list[IceServer]:
    """The server allocates its own relay on coturn; aiortc uses a single TURN server."""
    if settings.turn_host is None:
        return []
    credential = issue_credential(settings, now)
    return [
        IceServer(
            urls=turn_urls(settings.turn_host)[0],
            username=credential.username,
            credential=credential.password,
        )
    ]


async def open_connection(
    sdp: str, sdp_type: str, ice_servers: list[IceServer], connection_timeout_secs: int
) -> SmallWebRTCConnection:
    """Answer the caller's offer. The answer carries the server's gathered candidates."""
    connection = SmallWebRTCConnection(
        ice_servers=ice_servers, connection_timeout_secs=connection_timeout_secs
    )
    try:
        await connection.initialize(sdp=sdp, type=sdp_type)
    except BaseException:
        await connection.disconnect()
        raise
    return connection


def audio_transport(connection: SmallWebRTCConnection) -> SmallWebRTCTransport:
    return SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
