"""What keeps the console's pages to the browser that asked for them: the headers on every
response, the anti-forgery check of every form sent, and the reading of a form's fields."""

import hmac
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Nothing inline and nothing from another origin: scripts, styles and fonts are the console's
# own static files.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self';"
    " connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none';"
    " frame-ancestors 'none'"
)
HEADERS = {
    b"cache-control": b"no-store",
    b"content-security-policy": CONTENT_SECURITY_POLICY.encode("ascii"),
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"no-referrer",
    b"x-frame-options": b"DENY",
}
# A sign-in form with a long password is well under this; a longer body is refused unread.
MAX_FORM_BYTES = 4096
FORM_TYPE = "application/x-www-form-urlencoded"
# What a browser says of a request it sends from the console's own pages, or typed in.
OWN_SITE = ("same-origin", "none")


class ConsoleHeaders:
    """Sets the console's headers on every response of the app it wraps, whatever made it: a
    page, a static file, a redirect, an error handler or the server-error fallback. A header of
    the same name that the response had is replaced."""

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


class Form:
    """The fields of a URL-encoded form, as the bytes that were sent."""

    def __init__(self, pairs: list[tuple[bytes, bytes]]) -> None:
        self._pairs = pairs

    def one(self, name: bytes) -> bytes | None:
        """The field's value; None when it is missing or given more than once."""
        values = [value for key, value in self._pairs if key == name]
        return values[0] if len(values) == 1 else None

    def both(self, first: bytes, second: bytes) -> tuple[bytes, bytes] | None:
        """Two fields' values; None unless each is given exactly once."""
        values = (self.one(first), self.one(second))
        return (values[0], values[1]) if values[0] is not None and values[1] is not None else None


def tokens_match(supplied: bytes | None, expected: str | None) -> bool:
    """Whether the form's token is the expected one, compared in constant time. A missing or
    empty token never matches."""
    if not (supplied and expected):
        return False
    return hmac.compare_digest(supplied, expected.encode("utf-8"))


def from_another_site(request: Request) -> bool:
    """Whether the browser says the request comes from a page of another site. A browser that
    says nothing (an old one, or a test client) is judged by the token and the cookie alone."""
    site = request.headers.get("sec-fetch-site")
    return site is not None and site not in OWN_SITE


async def read_form(request: Request) -> Form | None:
    """The form the request sends; None for a body that is not a URL-encoded form or is
    longer than MAX_FORM_BYTES, which is not read further."""
    if request.headers.get("content-type", "").split(";")[0].strip() != FORM_TYPE:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    return Form(parse_qsl(bytes(body), keep_blank_values=True))
