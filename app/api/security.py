"""What keeps the admin API to the browser it serves: the headers on every answer, the one other
origin it answers (CORS), and the checks a browser session's request passes.

The console runs at its own origin (CONSOLE_ORIGIN), on a sibling of the API's host on the same
site. Other apps live on sibling hosts of that site too, so a cookie's SameSite rule does not
keep their pages out: a request a session may change state with must carry the session's
anti-forgery token and come from the console's origin, and a session is honoured only for a
request from that origin or one that names none (the browser names none for its own same-origin
reads). Without CONSOLE_ORIGIN, the API's own origin stands in for it, and no other origin is
answered.
"""

import hmac

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADERS = {
    b"cache-control": b"no-store",
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"no-referrer",
    b"content-security-policy": b"default-src 'none'; frame-ancestors 'none'",
}
# What the console may send from its origin, and how long a browser keeps the answer.
ALLOWED_METHODS = ("GET", "POST", "DELETE")
ALLOWED_HEADERS = ("Content-Type", "X-CSRF-Token")
PREFLIGHT_SECONDS = 600
# What the console may read of an answer beyond the safelisted headers: when to try again.
EXPOSED_HEADERS = "Retry-After"
CSRF_HEADER = "x-csrf-token"


class AdminHeaders:
    """Sets the admin headers on every response of the app it wraps, whatever made it: a
    route, a preflight, an error handler or the server-error fallback. A header of the same
    name that the response had is replaced."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in HEADERS
                ]
                message = {**message, "headers": [*kept, *HEADERS.items()]}
            await send(message)

        await self._app(scope, receive, send_with_headers)


class ConsoleCors:
    """CORS for exactly one origin, with credentials; no header for any other, and none at all
    when there is no origin to answer. A preflight is answered here, before any sign-in check:
    the browser sends it without credentials."""

    def __init__(self, app: ASGIApp, origin: str | None) -> None:
        self._app = app
        self._origin = origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._origin is None:
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            await self._preflight(origin, headers)(scope, receive, send)
            return
        answered = origin == self._origin

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers.add_vary_header("Origin")
                if answered:
                    response_headers.update(self._allowed())
                    response_headers["Access-Control-Expose-Headers"] = EXPOSED_HEADERS
            await send(message)

        await self._app(scope, receive, send_with_cors)

    def _allowed(self) -> dict[str, str]:
        if self._origin is None:
            raise RuntimeError("no origin to answer")
        return {
            "Access-Control-Allow-Origin": self._origin,
            "Access-Control-Allow-Credentials": "true",
        }

    def _preflight(self, origin: str | None, headers: Headers) -> Response:
        requested = {
            name.strip().lower()
            for name in headers.get("access-control-request-headers", "").split(",")
            if name.strip()
        }
        if (
            origin != self._origin
            or headers["access-control-request-method"] not in ALLOWED_METHODS
            or not requested <= {name.lower() for name in ALLOWED_HEADERS}
        ):
            return Response(status_code=400, headers={"Vary": "Origin"})
        return Response(
            status_code=204,
            headers={
                **self._allowed(),
                "Access-Control-Allow-Methods": ", ".join(ALLOWED_METHODS),
                "Access-Control-Allow-Headers": ", ".join(ALLOWED_HEADERS),
                "Access-Control-Max-Age": str(PREFLIGHT_SECONDS),
                "Vary": "Origin",
            },
        )


def console_origin(request: Request, configured: str | None) -> str:
    """The origin a browser session acts from: CONSOLE_ORIGIN, or the API's own."""
    if configured is not None:
        return configured
    return f"{request.url.scheme}://{request.url.netloc}"


def from_console(request: Request, configured: str | None) -> bool:
    """Whether the request comes from the console's origin, as its Origin header says."""
    return request.headers.get("origin") == console_origin(request, configured)


def tokens_match(supplied: str | None, expected: str) -> bool:
    """Whether the request's anti-forgery token is the session's, compared in constant time. A
    missing or empty token never matches."""
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
