"""What text that code speaks or staff read may not hold: one check for every such text."""

import unicodedata

# Unicode categories never in text a caller gives to be stored or spoken: controls (C0 with
# NUL, DEL, C1), format characters (bidi overrides, embeddings and isolates, zero-width
# characters, soft hyphens, the byte order mark), surrogates, private use, unassigned code
# points, and line and paragraph separators. Unseen, or able to make text read otherwise than
# it is stored.
HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def plain_text(value: str, *, digits: bool) -> str:
    """The text, unless it holds a character of HIDDEN_CATEGORIES or, where `digits` is false,
    a digit of any script: then ValueError."""
    for char in value:
        if unicodedata.category(char) in HIDDEN_CATEGORIES:
            raise ValueError("the text must not contain control or format characters")
        if not digits and char.isdigit():
            raise ValueError("the text must not contain digits")
    return value
