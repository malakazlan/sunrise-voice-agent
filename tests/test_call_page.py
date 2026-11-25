"""The call page (web/index.html), its headers and its fonts.

R1: the page is served with a Content-Security-Policy made from the page itself, and the other
    headers.
R2: the page holds one inline script and one inline style, no inline style attribute or event
    handler, no remote URL, and never writes markup.
R3: only the two named font files are served; any other name is not found.
"""

import base64
import hashlib
import re
from pathlib import Path

import pytest
from conftest import offline_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.page import FONTS, page_headers
from app.main import create_app
from app.runtime_config import taking_calls

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE = REPO_ROOT / "web" / "index.html"


def calls_app() -> FastAPI:
    return create_app(offline_settings(), config=taking_calls())


def allowed(block: bytes) -> str:
    """A block's source in a policy, worked out here by hand."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(block).digest()).decode("ascii") + "'"


def between(page: bytes, opening: bytes, closing: bytes) -> bytes:
    start = page.index(opening) + len(opening)
    return page[start : page.index(closing, start)]


# R1
def test_page_headers_match_the_page() -> None:
    with TestClient(calls_app()) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.content
    assert page == PAGE.read_bytes()
    script = between(page, b"<script>", b"</script>")
    style = between(page, b"<style>", b"</style>")
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; "
        f"script-src {allowed(script)}; "
        f"style-src {allowed(style)}; "
        "font-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "media-src 'self' blob: mediastream:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "microphone=(self), camera=(), geolocation=()"
    assert response.headers["cache-control"] == "no-cache"


# R1: the policy follows the page, and a page it cannot describe is refused.
def test_the_policy_follows_the_page() -> None:
    page = b"<style>a{}</style><script>one()</script>"
    changed = b"<style>a{}</style><script>two()</script>"
    assert allowed(b"one()") in page_headers(page)["Content-Security-Policy"]
    assert allowed(b"one()") not in page_headers(changed)["Content-Security-Policy"]
    assert allowed(b"two()") in page_headers(changed)["Content-Security-Policy"]
    for broken in (
        b"<style>a{}</style>",
        b"<script>one()</script>",
        b"<style>a{}</style><script>one()</script><script>two()</script>",
        b"<style>a{}</style><style>b{}</style><script>one()</script>",
    ):
        with pytest.raises(ValueError, match="exactly one inline"):
            page_headers(broken)


# R2
def test_page_is_self_contained_and_writes_text_only() -> None:
    page = PAGE.read_text(encoding="utf-8")
    # One script and one style, both inline and bare: the policy allows these two by hash.
    assert re.findall(r"<script\b[^>]*>", page) == ["<script>"]
    assert re.findall(r"<style\b[^>]*>", page) == ["<style>"]
    markup = page.replace(between(page.encode(), b"<script>", b"</script>").decode(), "")
    markup = markup.replace(between(page.encode(), b"<style>", b"</style>").decode(), "")
    # No inline style attribute and no inline event handler: the policy would refuse both.
    assert re.findall(r"<[^>]*\sstyle\s*=", markup) == []
    assert re.findall(r"<[^>]*\son[a-z]+\s*=", markup) == []
    # Nothing from another origin, and no link out: a reference is to a font of ours or to a
    # drawing in the page itself.
    assert re.findall(r"https?:|//[a-z]", page) == []
    assert re.findall(r"""(?:src|href)\s*=\s*["'](?!/fonts/|#[a-z])""", markup) == []
    # The microphone is asked for with the browser's echo cancellation and noise suppression.
    assert "echoCancellation: true, noiseSuppression: true, autoGainControl: true" in page
    # What the server sends is written as text, never as markup.
    for writer in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert writer not in page
    # Every font the page asks for is one the service serves, and the other way round.
    assert sorted(re.findall(r'url\("/fonts/([^"]+)"\)', page)) == sorted(FONTS)


# R3
def test_only_the_named_fonts_are_served() -> None:
    with TestClient(calls_app()) as client:
        for name in ("ibm-plex-sans-latin-wght.woff2", "ibm-plex-mono-latin-400.woff2"):
            response = client.get(f"/fonts/{name}")
            assert response.status_code == 200, name
            assert response.headers["content-type"] == "font/woff2"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.content == (REPO_ROOT / "web" / "fonts" / name).read_bytes()
            assert response.content[:4] == b"wOF2"
        for name in (
            "OFL-ibm-plex-sans.txt",
            "index.html",
            "..%2Findex.html",
            "%2e%2e%2f%2e%2e%2fpyproject.toml",
            "ibm-plex-sans-latin-wght.woff2%00",
            "nothing.woff2",
        ):
            assert client.get(f"/fonts/{name}").status_code == 404, name
        assert client.get("/fonts/").status_code == 404
