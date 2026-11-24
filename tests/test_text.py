import pytest

from app.guard.text import plain_text

# Written by hand: text that code speaks or staff read may not hold a control character (C0, DEL
# or C1), a format character (zero-width, soft hyphen, byte order mark, bidi overrides,
# embeddings and isolates), a surrogate, a private-use or unassigned code point, or a line or
# paragraph separator. Escapes only: none of them is written as itself in this file.
HIDDEN = [
    "\x00",
    "a\tb",
    "a\nb",
    "a\x1fb",
    "a\x7fb",
    "a\x85b",
    "a\x9fb",
    "a­b",
    "a​b",
    "a‌b",
    "a‍b",
    "a‎b",
    "a‏b",
    "a‪b",
    "a‮b",
    "a⁦b",
    "a⁩b",
    "a⁠b",
    "a﻿b",
    "a\ud800b",
    "ab",
    "a͸b",
    "a b",
    "a b",
]
VISIBLE = [
    "",
    "Ana Diaz",
    "José Núñez-O'Brien",
    "Zoë 李 محمد",
    "call me at 555-201-3344, after 2 PM",
    "a b",
]


def test_hidden_characters_are_refused() -> None:
    for text in HIDDEN:
        for digits in (False, True):
            with pytest.raises(ValueError, match="control or format"):
                plain_text(text, digits=digits)
    assert [plain_text(text, digits=True) for text in VISIBLE] == VISIBLE
    # Digits of any script, when asked.
    for text in ("call me at 555", "a٣b"):
        with pytest.raises(ValueError, match="digits"):
            plain_text(text, digits=False)
    assert plain_text("Ana Diaz", digits=False) == "Ana Diaz"
