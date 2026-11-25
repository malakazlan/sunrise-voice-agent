"""The call page and its fonts.

Both are read once, when the app is built, and served by coroutines: no file is read while a
call runs and no handler goes to the thread pool (app/api/offer.py). The page carries one
inline script and one inline style; its Content-Security-Policy allows exactly those two, by
their SHA-256, and nothing from another origin. The fonts are a closed set of names: any other
name is not found, so a path cannot be asked for."""

import base64
import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

WEB = Path(__file__).resolve().parents[2] / "web"
CALL_PAGE = WEB / "index.html"
# IBM Plex (SIL Open Font License; the licence texts are beside the files).
FONTS = ("ibm-plex-sans-latin-wght.woff2", "ibm-plex-mono-latin-400.woff2")
FONT_SECONDS = 86_400

_SCRIPT = re.compile(rb"<script>(.*?)</script>", re.DOTALL)
_STYLE = re.compile(rb"<style>(.*?)</style>", re.DOTALL)


def _only(pattern: re.Pattern[bytes], page: bytes, what: str) -> bytes:
    """The page's one inline block of a kind; a page with none or several is refused."""
    blocks = pattern.findall(page)
    if len(blocks) != 1:
        raise ValueError(f"the call page must hold exactly one inline {what}, not {len(blocks)}")
    block: bytes = blocks[0]
    return block


def _allowed(block: bytes) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(block).digest()).decode("ascii") + "'"


def page_headers(page: bytes) -> dict[str, str]:
    """The page's headers, its policy made from the page itself: a changed script or style
    changes the policy with it, and anything added inline or from elsewhere does not run."""
    policy = "; ".join(
        (
            "default-src 'none'",
            f"script-src {_allowed(_only(_SCRIPT, page, 'script'))}",
            f"style-src {_allowed(_only(_STYLE, page, 'style'))}",
            "font-src 'self'",
            "connect-src 'self'",
            "img-src 'self' data:",
            "media-src 'self' blob: mediastream:",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        )
    )
    return {
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
        "Cache-Control": "no-cache",
    }


def build_page_router() -> APIRouter:
    page = CALL_PAGE.read_bytes()
    headers = page_headers(page)
    fonts = {name: (WEB / "fonts" / name).read_bytes() for name in FONTS}
    font_headers = {
        "Cache-Control": f"public, max-age={FONT_SECONDS}",
        "X-Content-Type-Options": "nosniff",
    }
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def call_page() -> Response:
        return Response(page, media_type="text/html", headers=headers)

    @router.get("/fonts/{name}", include_in_schema=False)
    async def font(name: str) -> Response:
        data = fonts.get(name)
        if data is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return Response(data, media_type="font/woff2", headers=font_headers)

    return router
