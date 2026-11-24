from app.guard.text import has_hidden_characters

# Written by hand: text that code speaks or staff read may not hold a control character (C0, DEL
# or C1) or a format character (zero-width, soft hyphen, byte order mark, bidi overrides,
# embeddings and isolates). Escapes only: none of them is written as itself in this file.
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
]
VISIBLE = [
    "",
    "Ana Diaz",
    "José Núñez-O'Brien",
    "Zoë 李 محمد",
    "call me at 555-201-3344, after 2 PM",
    "a b",
]


def test_hidden_characters_are_found() -> None:
    assert [text for text in HIDDEN if not has_hidden_characters(text)] == []
    assert [text for text in VISIBLE if has_hidden_characters(text)] == []
